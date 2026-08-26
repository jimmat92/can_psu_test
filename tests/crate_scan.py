#!/usr/bin/env python3
"""
Full characterisation sweep of an ELMB PSU crate. Run this after
tests/smoke_test.py has passed -- it assumes the node id is right, the bus
works and the server config is good, and spends its time on the crate itself.

    1. measure everything as found (all 64 analog inputs + the DO word)
    2. switch every branch OFF, settle, measure again, confirm they went off
    3. switch every branch ON,  settle, measure again, confirm they came on
    4. measure N more times and reduce to mean / variance / stdev per channel

From those four data sets it answers three questions:

    * which slots hold a module.  A module spans TWO branches -- slot s owns
      branches 2s and 2s+1 -- so every finding is reported against the slot,
      not the branch. A fault on branch 1 is a fault of the module that is
      branches 0 AND 1.
    * does the on/off command actually reach the hardware, on both the
      command path (DO read-back) and the physical path (rail voltage).
    * do the sensors work, and how steady are they (pass/fail on stdev).

How presence is decided (docs/REFERENCE.md s4). The two input types fail
differently, and that is the whole trick:

    voltage input   100:1 divider to ground -- reads ~0 whenever no rail is
                    present, whether the branch is off or the slot is empty.
                    Useless on its own for presence.
    current input   high impedance, held at 2.5 V by the sensor's own zero
                    reference. That reference is powered by the module's
                    housekeeping supply, NOT by the switched rail, so it
                    keeps sitting at 2.5 V with the branch switched off.
                    With no module in the slot nothing drives the line at
                    all and it floats at a few hundred mV.

So: "current input parked at 2.5 V" == a module is in that slot and its
monitoring electronics are alive. That test is valid in the OFF state as
well as the ON state, which is why step 2 is a measurement and not just a
switch test.

The server is started as a context manager, so it is stopped on every exit
path -- success, a failed check, Ctrl-C or any exception. Branch states are
latched in the ELMB and are NOT affected by the server stopping; the crate
is left with every branch ON unless --restore-as-found is given.

NOTE: this switches all sixteen branches off and back on. Anything powered
from the crate is power-cycled. Use --skip-switch-test for a read-only run.
"""

import argparse
import contextlib
import json
import statistics
import socket
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

try:
    from elmbpsu_server import OpcUaServer, ServerError
    from elmbpsu_opcua import (PsuCrate, NS_URI, branch_channels, branch_label,
                               adc_volts, to_volts, to_amps)
    from asyncua.sync import Client
except ImportError as exc:
    sys.exit(f"error: cannot import required modules ({exc}).\n"
             "       activate the venv first: source .venv/bin/activate")

QUANTITIES = (("canv", "CAN V", "V"), ("cani", "CAN I", "A"),
              ("adv", "AD V", "V"), ("adi", "AD I", "A"))


# --------------------------------------------------------------- config
def bus_interval_s(config_file, attr, fallback=10.0):
    """A Bus/@... interval from the server config, in seconds.

    syncIntervalMs matters because TPDO3 is transportMechanism="sync", so the
    analog cache only refreshes once per SYNC -- read it faster than that and
    you get the same numbers back, with a variance of exactly zero, which
    would be a lie. nodeGuardIntervalMs matters because stateAsText is only
    populated on a node-guard cycle, so it sets how long a freshly started
    server answers BadWaitingForInitialData."""
    try:
        root = ET.parse(config_file).getroot()
    except Exception:
        return fallback
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "Bus" and el.get(attr):
            try:
                return int(el.get(attr)) / 1000.0
            except ValueError:
                break
    return fallback


# ---------------------------------------------------------- measurement
def wait_for_new_scan(crate, nodes, prev, sync_s, source, margin=1.5, poll=0.5,
                      timeout=None):
    """Return (samples, confirmed) once the analog cache holds a scan taken
    after this call started.

    Preferred evidence is the SourceTimestamp of every channel advancing past
    what `prev` held -- that is positive proof of a new TPDO3 scan. If the
    server does not move the timestamps we fall back to waiting out a whole
    SYNC period plus a margin, which gets fresh data anyway but cannot prove
    it, and comes back confirmed=False so the caller can say so.

    With no `prev` to compare against -- the first scan of a run -- "fresh"
    can only mean "the cache has been filled at all". Until the first SYNC
    reaches the server every channel reads BadWaitingForInitialData, so this
    waits for the channels to become readable rather than returning 64 empty
    samples."""
    if source == "sdo":
        return crate.read_ai_all(source, nodes), True     # on-request, never cached
    if timeout is None:
        timeout = sync_s + margin
    deadline = time.monotonic() + timeout
    prev_ts = [s.timestamp for s in prev] if prev else None
    samples = None
    while time.monotonic() < deadline:
        samples = crate.read_ai_all(source, nodes)
        if prev_ts is None:
            if all(s.ok for s in samples):
                return samples, True
        elif all(s.timestamp is not None and p is not None and s.timestamp != p
                 for s, p in zip(samples, prev_ts)):
            return samples, True
        time.sleep(poll)
    if samples is None:
        samples = crate.read_ai_all(source, nodes)
    return samples, False


def measure(crate, nodes, prev, sync_s, source, label, quiet=False, timeout=None):
    t0 = time.monotonic()
    samples, confirmed = wait_for_new_scan(crate, nodes, prev, sync_s, source,
                                           timeout=timeout)
    if not quiet:
        if prev is None:
            how = "cache filled" if confirmed else \
                  "gave up waiting for every channel to become readable"
        else:
            how = "new scan confirmed by timestamp" if confirmed else \
                  "timestamps did not move -- waited a full SYNC period instead"
        bad = sum(1 for s in samples if not s.ok)
        print(f"      {label}: {time.monotonic() - t0:.1f}s ({how})"
              + (f", {bad} channel(s) unreadable" if bad else ""))
    return samples


def branch_view(samples, branch):
    """The four samples belonging to one branch, keyed canv/adv/cani/adi."""
    return {k: samples[c] for k, c in branch_channels(branch).items()}


# ------------------------------------------------------- classification
def classify_current(sample, tol):
    """What a current-sense input is telling us, judged at the ADC pin rather
    than after conversion -- see the module docstring."""
    if not sample.ok:
        return "unreadable"
    v = adc_volts(sample.uv)
    if abs(v - 2.5) <= tol:
        return "zero"          # sensor present, powered, carrying ~no current
    if v < 2.5 - tol:
        return "undriven"      # nothing holding the line up: no module / open contact
    return "implausible"       # above the zero reference on an unloaded rail


def classify_voltage(sample, on_min, off_max):
    if not sample.ok:
        return "unreadable"
    v = to_volts(sample.uv)
    if v >= on_min:
        return "up"
    if v <= off_max:
        return "down"
    return "partial"           # neither clearly a rail nor clearly nothing


# ------------------------------------------------------------- printing
def print_measurement(samples, title, args):
    print(f"\n  {title}")
    print(f"    {'br':>2} {'slot':>4} {'pos':>3} "
          + "".join(f"{lbl:>10} " for _, lbl, _ in QUANTITIES))
    for b in range(16):
        view = branch_view(samples, b)
        cells = []
        for key, _, unit in QUANTITIES:
            s = view[key]
            if not s.ok:
                cells.append(f"{'unreadable':>11}")
                continue
            if unit == "V":
                cells.append(f"{to_volts(s.uv):9.3f}V ")
            else:
                flag = " " if classify_current(s, args.i_zero_tol) == "zero" else "!"
                cells.append(f"{to_amps(s.uv):9.3f}A{flag}")
        print(f"    {b:>2} {b // 2:>4} {'AB'[b % 2]:>3} " + "".join(cells))
    print("    ! = current input not parked at its 2.5 V zero "
          "(nothing driving the line, or a real current)")


def print_stats(stats, args, only=None):
    print("    mean +/- stdev:")
    print(f"    {'br':>2} " + "".join(f"{lbl:^20}" for _, lbl, _ in QUANTITIES))
    for b in range(16):
        if only is not None and b not in only:
            continue
        cells = []
        for key, _, unit in QUANTITIES:
            st = stats[b][key]
            if st is None:
                cells.append(f"{'unreadable':^20}")
            else:
                cells.append(f"{st['mean']:9.3f}{unit} +/-{st['stdev']:5.3f} ")
        print(f"    {b:>2} " + "".join(cells))


# ------------------------------------------------------------- analysis
def reduce_samples(series, args):
    """series: list of measurements (each a list of 64 AiSample).
    Returns stats[branch][key] = dict(mean, variance, stdev, min, max, ptp, n)
    in engineering units, or None if the channel was never readable."""
    stats = {}
    for b in range(16):
        stats[b] = {}
        for key, _, unit in QUANTITIES:
            ch = branch_channels(b)[key]
            conv = to_volts if unit == "V" else to_amps
            vals = [conv(m[ch].uv) for m in series if m[ch].ok]
            if not vals:
                stats[b][key] = None
                continue
            n = len(vals)
            var = statistics.variance(vals) if n > 1 else 0.0
            stats[b][key] = {
                "n": n, "dropped": len(series) - n,
                "mean": statistics.mean(vals), "variance": var,
                "stdev": var ** 0.5, "min": min(vals), "max": max(vals),
                "ptp": max(vals) - min(vals),
                "adc_mean": statistics.mean(
                    [adc_volts(m[ch].uv) for m in series if m[ch].ok]),
            }
    return stats


def analyse(data, stats, args):
    """Everything the numbers support, as a dict the report and the JSON
    both render. Slot-level, because a module spans two branches."""
    off, on = data["off"], data["on"]
    on_value = 0 if args.invert else 1
    # Which branches were actually commanded on when the "on" measurement was
    # taken. Normally all sixteen -- but under --skip-switch-test nothing was
    # switched, so a rail that is down may simply be a branch the operator had
    # left off, and calling that a fault would be wrong.
    word_on = data.get("word_on_state", 0xFFFF if on_value else 0x0000)
    slots = {}
    for s in range(8):
        branches = (2 * s, 2 * s + 1)
        expect_up = [b for b in branches if ((word_on >> b) & 1) == on_value]
        n_expect = 2 * len(expect_up)          # two rails per branch
        cur, volt = {}, {}
        for b in branches:
            for state, meas in (("off", off), ("on", on)):
                if meas is None:
                    continue
                view = branch_view(meas, b)
                for key in ("cani", "adi"):
                    cur[(b, key, state)] = classify_current(view[key],
                                                            args.i_zero_tol)
                for key in ("canv", "adv"):
                    volt[(b, key, state)] = classify_voltage(
                        view[key], args.v_on_min, args.v_off_max)

        # Presence: a current input parked at 2.5 V in EITHER state means the
        # module's own electronics are alive in that slot.
        alive = sorted({(b, k) for (b, k, _), v in cur.items() if v == "zero"})
        undriven = sorted({(b, k) for (b, k, _), v in cur.items()
                           if v in ("undriven", "implausible")}) if cur else []
        undriven = [x for x in undriven if x not in alive]
        rails_up = sorted({(b, k) for (b, k, st), v in volt.items()
                           if st == "on" and v == "up"})

        if not alive and not rails_up:
            verdict, detail = "ABSENT", "no module (nothing drives its sense lines)"
        elif len(alive) == 4 and len(rails_up) == n_expect:
            verdict = "POPULATED"
            detail = ("all four sensors alive; no branch was commanded on, so "
                      "the rails were not judged" if n_expect == 0 else
                      f"all {n_expect} commanded rails up, all four sensors alive")
        elif alive and undriven:
            verdict = "POPULATED*"
            detail = (f"module present but {len(undriven)} of 4 sense lines are "
                      "not driven -- partially seated module or damaged harness")
        elif len(alive) == 4 and len(rails_up) < n_expect:
            verdict = "POPULATED*"
            detail = (f"sensors all alive but only {len(rails_up)} of {n_expect} "
                      "commanded rails came up -- output stage or rail fault")
        elif not alive and rails_up:
            verdict = "POPULATED*"
            detail = ("rails are up but no sensor holds its zero -- contradictory, "
                      "check the sense harness")
        else:
            verdict, detail = "POPULATED*", "mixed evidence, see the channel table"
        slots[s] = {"branches": list(branches), "verdict": verdict,
                    "detail": detail, "sensors_alive": len(alive),
                    "rails_up_on": len(rails_up), "rails_expected": n_expect,
                    "undriven": [f"branch {b} {k}" for b, k in undriven]}

    populated = [s for s in range(8) if slots[s]["verdict"] != "ABSENT"]

    # --- switching, per branch of a populated slot ------------------------
    switching = {}
    if off is not None and on is not None:
        for s in populated:
            for b in slots[s]["branches"]:
                vo, vn = branch_view(off, b), branch_view(on, b)
                rows = {}
                for key in ("canv", "adv"):
                    down = classify_voltage(vo[key], args.v_on_min, args.v_off_max)
                    up = classify_voltage(vn[key], args.v_on_min, args.v_off_max)
                    if down == "down" and up == "up":
                        r = "OK"
                    elif down != "down" and up == "up":
                        r = "STUCK ON (did not drop when switched off)"
                    elif down == "down" and up != "up":
                        r = "NO OUTPUT (did not come up when switched on)"
                    else:
                        r = f"UNCLEAR (off={down}, on={up})"
                    rows[key] = {"result": r,
                                 "off_v": to_volts(vo[key].uv) if vo[key].ok else None,
                                 "on_v": to_volts(vn[key].uv) if vn[key].ok else None}
                switching[b] = rows

    # --- sensor health, then stability ------------------------------------
    # Order matters. A sense line that nothing drives sits rock steady at a
    # few hundred mV, so it sails through any stdev test -- "steady" is not
    # "working". Plausibility is checked first, and only a channel that is
    # reporting something real gets judged on how steadily it reports it.
    # Entries are (verdict, why, is_its_own_fault). The last flag keeps a rail
    # the switching section already condemned from being counted a second time
    # as a sensor fault -- it is one defect seen from two angles.
    commanded = data.get("word_repeats", 0xFFFF if on_value else 0x0000)
    stability = {}
    for s in populated:
        for b in slots[s]["branches"]:
            branch_on = ((commanded >> b) & 1) == on_value
            for key, lbl, unit in QUANTITIES:
                st = stats[b][key]
                if st is None:
                    stability[(b, key)] = ("FAIL", "never readable", True)
                    continue
                if st["dropped"]:
                    stability[(b, key)] = (
                        "FAIL", f"{st['dropped']} of {len(data['repeats'])} reads "
                        "came back bad", True)
                    continue
                if unit == "A" and abs(st["adc_mean"] - 2.5) > args.i_zero_tol:
                    how = "nothing driving it" if st["adc_mean"] < 2.5 \
                          else "above the zero reference"
                    stability[(b, key)] = (
                        "FAIL", f"sense line reads {st['adc_mean']:.3f} V at the "
                        f"ADC pin, not the sensor's 2.5 V zero -- {how} "
                        f"({st['mean']:.3f} A is what that converts to, not a "
                        "real current)", True)
                    continue
                if unit == "V" and branch_on and st["mean"] < args.v_on_min:
                    already = switching.get(b, {}).get(key, {}).get("result", "OK")
                    stability[(b, key)] = (
                        "FAIL", f"branch is commanded ON but the rail reads "
                        f"{st['mean']:.3f} V"
                        + ("" if already == "OK" else " -- same defect as the "
                           "switching section reports"), already == "OK")
                    continue
                limit = args.v_stdev_max if unit == "V" else args.i_stdev_max
                if st["n"] < 2:
                    stability[(b, key)] = ("SKIP", "needs at least 2 samples", False)
                elif st["stdev"] > limit:
                    stability[(b, key)] = (
                        "FAIL", f"stdev {st['stdev']:.3f}{unit} > {limit}{unit} "
                        f"(var {st['variance']:.4f}, p-p {st['ptp']:.3f}{unit}, "
                        f"{st['min']:.3f} .. {st['max']:.3f}{unit})", True)
                else:
                    stability[(b, key)] = ("PASS", f"stdev {st['stdev']:.3f}{unit}",
                                           False)

    return {"slots": slots, "populated": populated,
            "switching": switching, "stability": stability}


def report(res, data, stats, args):
    """Print the findings and return the number of faults."""
    slots, populated = res["slots"], res["populated"]
    faults = []          # (slot, text)

    print("\n=== SLOT OCCUPANCY ===")
    print("  a module spans TWO branches: slot s owns branches 2s and 2s+1,")
    print("  so a fault on either branch is a fault of that one module.\n")
    print(f"  {'slot':>4} {'branches':>9} {'module':>11} "
          f"{'sensors':>8} {'rails on':>9}  note")
    for s in range(8):
        d = slots[s]
        print(f"  {s:>4} {str(d['branches'][0]) + ',' + str(d['branches'][1]):>9} "
              f"{d['verdict']:>11} {str(d['sensors_alive']) + '/4':>8} "
              f"{str(d['rails_up_on']) + '/' + str(d['rails_expected']):>9}"
              f"  {d['detail']}")
    print("\n  POPULATED* = a module is there but something about it is wrong.")

    print("\n=== SWITCHING ===")
    if data["off"] is None:
        print("  skipped (--skip-switch-test)")
    else:
        for want, got in data["do_checks"]:
            ok = "OK" if want == got else "*** MISMATCH ***"
            print(f"  DO word: wrote 0x{want:04X}, read back 0x{got:04X}   {ok}")
            if want != got:
                faults.append((None, f"DO read-back 0x{got:04X} != 0x{want:04X} -- "
                                     "the command never reached the outputs"))
        print("\n  physical response, populated slots only "
              "(an empty slot reads ~0 V in both states and proves nothing):")
        for b in sorted(res["switching"]):
            for key, lbl, _ in QUANTITIES:
                if key not in ("canv", "adv"):
                    continue
                r = res["switching"][b][key]
                off_v = "n/a" if r["off_v"] is None else f"{r['off_v']:7.3f}V"
                on_v = "n/a" if r["on_v"] is None else f"{r['on_v']:7.3f}V"
                mark = "" if r["result"] == "OK" else "   <<<"
                print(f"    branch {b:>2} {lbl:>5}:  off {off_v}   on {on_v}   "
                      f"{r['result']}{mark}")
                if r["result"] != "OK":
                    faults.append((b // 2, f"branch {b} {lbl}: {r['result']}"))

    print(f"\n=== SENSORS ({len(data['repeats'])} samples) ===")
    print("  a channel passes only if it reports something plausible AND does "
          "so steadily:")
    print(f"    plausible  current input within {args.i_zero_tol} V of its 2.5 V "
          "zero at the ADC pin;")
    print(f"               voltage input >= {args.v_on_min} V when its branch is "
          "commanded on")
    print(f"    steady     stdev <= {args.v_stdev_max} V / {args.i_stdev_max} A. "
          "variance = stdev^2 and is in the JSON.\n")
    print_stats(stats, args, only=set(
        b for s in populated for b in slots[s]["branches"]))
    npass = sum(1 for v in res["stability"].values() if v[0] == "PASS")
    nfail = sum(1 for v in res["stability"].values() if v[0] == "FAIL")
    print(f"\n  {npass} pass, {nfail} fail, of "
          f"{len(res['stability'])} channels on populated slots")
    for (b, key), (verdict, why, own) in sorted(res["stability"].items()):
        if verdict != "PASS":
            lbl = dict((k, l) for k, l, _ in QUANTITIES)[key]
            print(f"    {verdict}  branch {b:>2} {lbl:>5}: {why}")
            if verdict == "FAIL" and own:
                faults.append((b // 2, f"branch {b} {lbl}: {why}"))

    # Slot-level findings only where no channel-level fault already names the
    # slot, so a partially-seated module is not reported three times over.
    named = {s for s, _ in faults}
    for s in range(8):
        if slots[s]["verdict"] == "POPULATED*" and s not in named:
            faults.append((s, slots[s]["detail"]))

    print("\n=== VERDICT ===")
    absent = [s for s in range(8) if slots[s]["verdict"] == "ABSENT"]
    print(f"  modules present : {_slots_str(populated)}"
          f"{'' if not populated else '  (branches ' + _branches_str(populated) + ')'}")
    print(f"  modules absent  : {_slots_str(absent)}")
    if data["off"] is not None:
        cmd_ok = all(w == g for w, g in data["do_checks"])
        rails = [(b, k) for b, rows in res["switching"].items()
                 for k, r in rows.items() if r["result"] != "OK"]
        n_rails = sum(len(rows) for rows in res["switching"].values())
        lbls = dict((k, l) for k, l, _ in QUANTITIES)
        if cmd_ok:
            print("  command path    : PASS  (0x0000 and 0xFFFF both read back "
                  "from the DO latches)")
        else:
            print("  command path    : FAIL  (the DO latches did not take what "
                  "was written -- see above)")
        if not rails:
            print(f"  rails respond   : PASS  (all {n_rails} rails of every "
                  "populated slot went off and back on)")
        else:
            named = ", ".join(f"branch {b} {lbls[k]}" for b, k in sorted(rails))
            print(f"  rails respond   : FAIL on {len(rails)} of {n_rails} "
                  f"({named})")
            if cmd_ok:
                print("                    the command reached the latches, so "
                      "this is the rail, not the switch.")
    print(f"  sensor channels : {npass} pass / {nfail} fail")

    suspect = sorted({s for s, _ in faults if s is not None})
    if not faults:
        print("\n  no faults found.")
    else:
        print(f"\n  FAULTS ({len(faults)}):")
        for s, text in faults:
            where = "crate/server" if s is None else \
                f"slot {s} (module = branches {2 * s} and {2 * s + 1})"
            print(f"    - {where}: {text}")
        print(f"\n  SUSPECT MODULES: {_slots_str(suspect)} -- these are the "
              "modules to pull,")
        print("  not the individual branches. Both branches of a listed slot "
              "belong to one module.")
        _print_interpretation(res, suspect)
    return len(faults)


def _slots_str(slots):
    return ", ".join(f"slot {s}" for s in slots) if slots else "none"


def _branches_str(slots):
    return ", ".join(f"{2 * s}-{2 * s + 1}" for s in slots)


def _print_interpretation(res, suspect):
    """Which side of the connector a fault is on. The current sensors and
    their 2.5 V references live ON THE MODULE, not on the crate backplane --
    docs/REFERENCE.md s4 measured it: pulling a module makes its sense lines
    float, while merely switching its branch off leaves them parked at 2.5 V.
    A crate-side sensor would keep its reference either way."""
    sense_faults = [s for s in suspect if res["slots"][s]["undriven"]]
    print("\n  Where the fault sits (the sense electronics are on the MODULE, "
          "not the crate --")
    print("  docs/REFERENCE.md s4: pulling a module makes its sense lines float, "
          "switching")
    print("  its branch off does not, which a crate-side sensor could not do):")
    if sense_faults:
        plural = "s" if len(sense_faults) > 1 else ""
        print(f"    Slot{plural} {', '.join(str(s) for s in sense_faults)} "
              f"ha{'ve' if plural else 's'} sense lines that nothing drives while")
        print("    other channels of the same module are fine. A module that was "
              "simply dead")
        print("    could not produce the good channels, so this is a CONTACT "
              "problem, at the")
        print("    module-to-backplane connector or in the harness.")
        print("      1. reseat the module and re-run this script.")
        print("      2. if it persists, move that module to a slot this run "
              "called healthy:")
        print("         fault follows the module -> the module is bad;")
        print("         fault stays with the slot -> the crate backplane is bad.")
    else:
        print("    No undriven sense lines: every module that is present is "
              "reporting.")
        print("    Faults listed above are rail/output or stability problems, "
              "which are on")
        print("    the module's own converter -- swap-test the same way if you "
              "need proof.")


# ----------------------------------------------------------------- main
def switch_all(crate, on, args):
    """Drive every branch to one state and read the latches back. Writes the
    whole 16-bit word rather than the per-branch Booleans: RPDO1.branchNN is
    a quasar RpdoCachedVariable and the first per-branch write after a server
    start transmits the server's zeroed shadow cache instead of your intent
    (docs/REFERENCE.md s6)."""
    on_value = 0 if args.invert else 1
    word = 0xFFFF if bool(on) == bool(on_value) else 0x0000
    if args.method == "rpdo":
        crate.write_word(word)
    else:
        for b in range(16):
            crate.write_branch_sdo(b, 1 if bool(on) == bool(on_value) else 0)
    time.sleep(args.settle)
    return word, crate.do_word()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-n", "--samples", type=int, default=5,
                   help="repeat measurements for the mean/variance stage (default 5)")
    p.add_argument("--settle", type=float, default=2.0,
                   help="seconds to wait after a switch before reading back (default 2)")
    p.add_argument("--skip-switch-test", action="store_true",
                   help="read-only: no branch is switched (steps 2 and 3 skipped)")
    p.add_argument("--restore-as-found", action="store_true",
                   help="put the DO word back as it was instead of leaving all on")
    p.add_argument("--source", choices=["tpdo", "sdo"], default="tpdo",
                   help="tpdo = SYNC-driven cache (default); sdo returns Bad on this crate")
    p.add_argument("--method", choices=["rpdo", "sdo"], default="rpdo")
    p.add_argument("--invert", action="store_true",
                   help="old (pre-2.0.0) PSU: output level 0 means ON")
    p.add_argument("--json", metavar="PATH",
                   help="write every raw sample and all statistics here")

    g = p.add_argument_group("pass/fail thresholds")
    g.add_argument("--v-on-min", type=float, default=10.0,
                   help="rail volts at or above this count as ON (default 10.0)")
    g.add_argument("--v-off-max", type=float, default=1.0,
                   help="rail volts at or below this count as OFF (default 1.0)")
    g.add_argument("--i-zero-tol", type=float, default=0.25,
                   help="volts at the ADC pin either side of the sensor's 2.5 V "
                        "zero still counted as 'sensor alive' (default 0.25, "
                        "i.e. +/-2 A). Assumes nothing is drawing real current.")
    g.add_argument("--v-stdev-max", type=float, default=0.05,
                   help="stability limit for voltage channels (default 0.05 V)")
    g.add_argument("--i-stdev-max", type=float, default=0.05,
                   help="stability limit for current channels (default 0.05 A)")

    g = p.add_argument_group("server and endpoint")
    g.add_argument("--use-running-server", action="store_true",
                   help="attach to a server that is already up instead of "
                        "starting and stopping one")
    g.add_argument("--endpoint", default=f"opc.tcp://{socket.gethostname()}:48012")
    g.add_argument("--bus", default="psuCtrlBus")
    g.add_argument("--node", default="psuCrate1")
    g.add_argument("--config-file", default=None,
                   help="default: $CAN_PSU_CONFIG/config-elmbpsu.xml")
    g.add_argument("--opcua-backend-config", default=None)
    g.add_argument("--no-sudo", action="store_true")
    g.add_argument("--start-timeout", type=float, default=15.0,
                   help="how long to wait for the OPC-UA endpoint to open")
    g.add_argument("--warmup", type=float, default=None,
                   help="how long to wait after that for the crate itself to "
                        "answer -- the server returns BadWaitingForInitialData "
                        "until it has read each node from the ELMB once "
                        "(default: nodeGuardIntervalMs + syncIntervalMs + 10s)")
    g.add_argument("--sync-interval-ms", type=float, default=None,
                   help="override the Bus/@syncIntervalMs read from the config")
    args = p.parse_args()

    if args.samples < 2:
        print("warning: --samples < 2, so there is nothing to take a variance of")

    server = OpcUaServer(config_file=args.config_file,
                         opcua_backend_config=args.opcua_backend_config,
                         use_sudo=not args.no_sudo)
    sync_s = (args.sync_interval_ms / 1000.0 if args.sync_interval_ms is not None
              else bus_interval_s(server.config_file, "syncIntervalMs"))
    nodeguard_s = bus_interval_s(server.config_file, "nodeGuardIntervalMs")
    if args.source == "sdo":
        sync_s = 0.0
    if args.warmup is None:
        # Worst case the crate answers one node-guard cycle after the server
        # opens, and the first TPDO3 scan lands one SYNC after that.
        args.warmup = max(30.0, nodeguard_s + sync_s + 10.0)

    n_scans = args.samples + (1 if args.skip_switch_test else 3)
    est = (n_scans * (sync_s + 1.5) + nodeguard_s
           + (0 if args.skip_switch_test else 2 * args.settle))
    print(f"plan: {n_scans} analog scans of all 64 channels, "
          f"~{est:.0f}s plus server start-up")
    print(f"      TPDO3 refreshes once per SYNC and Bus/@syncIntervalMs is "
          f"{sync_s * 1000:.0f} ms, so each")
    print("      scan costs at least that much wall time.")
    if sync_s >= 5.0:
        print(f"      tip: set syncIntervalMs to 1000 in "
              f"{Path(server.config_file).name} and restart the server")
        print("           to run this roughly ten times faster.")
    if not args.skip_switch_test:
        print("      NOTE: every branch will be switched OFF and back ON. "
              "Anything powered")
        print("            from this crate is power-cycled. "
              "--skip-switch-test avoids that.")

    with contextlib.ExitStack() as stack:
        if args.use_running_server:
            if not server.is_running():
                sys.exit("error: --use-running-server but no CanOpenOpcUa is "
                         f"running for {server.config_file}")
            print(f"\n[0/4] attaching to the running server, pid {server.pid()}")
        else:
            stack.enter_context(server)
            print(f"\n[0/4] server started, pid {server.pid()}")
            if not server.wait_ready(timeout=args.start_timeout):
                sys.exit("error: OPC-UA endpoint never opened -- check server.log")
            print("      endpoint open")

        client = Client(url=args.endpoint)
        client.connect()
        stack.callback(client.disconnect)
        try:
            ns = client.get_namespace_index(NS_URI)
        except Exception:
            ns = 2
        crate = PsuCrate(client, ns, args.bus, args.node)
        nodes = crate.ai_nodes(args.source)

        # The endpoint being open is not the crate answering. Every node reads
        # BadWaitingForInitialData until the server has fetched it from the
        # ELMB once, and stateAsText waits on a node-guard cycle.
        print(f"      waiting up to {args.warmup:.0f}s for the crate to answer "
              f"(node guard runs every {nodeguard_s:.0f}s) ...")
        ok, state = crate.wait_ready(timeout=args.warmup)
        if not ok:
            sys.exit(f"error: the crate never answered: {state}\n"
                     "       check --bus/--node against config-elmbpsu.xml, and "
                     "server.log for the node table")
        print(f"      NMT state {state}, DO word 0x{crate.do_word():04X}")
        if args.method == "rpdo" and state != "OPERATIONAL":
            sys.exit(f"error: node is {state}; RPDO writes are only acted on in "
                     "OPERATIONAL. Fix requestedState, or use --method sdo.")

        data = {"do_checks": [], "off": None, "on": None}

        print("\n[1/4] measuring as found ...")
        data["word_as_found"] = crate.do_word()
        # First scan gets a longer budget: the TPDO3 cache is empty until the
        # server's first SYNC, which can be a whole syncIntervalMs away.
        data["baseline"] = measure(crate, nodes, None, sync_s, args.source,
                                   "baseline", timeout=max(args.warmup,
                                                           sync_s + 5.0))
        print_measurement(data["baseline"], "as found", args)

        if not args.skip_switch_test:
            print("\n[2/4] switching every branch OFF ...")
            want, got = switch_all(crate, False, args)
            data["do_checks"].append((want, got))
            print(f"      wrote 0x{want:04X}, read back 0x{got:04X}"
                  f"{'' if want == got else '   *** MISMATCH ***'}")
            data["off"] = measure(crate, nodes, data["baseline"], sync_s,
                                  args.source, "off-state scan")
            print_measurement(data["off"], "all branches OFF", args)
            n_up = sum(1 for b in range(16) for k in ("canv", "adv")
                       if classify_voltage(branch_view(data["off"], b)[k],
                                           args.v_on_min, args.v_off_max) == "up")
            print(f"      rails still up: {n_up}"
                  + ("  <- these did not switch off" if n_up else "  (all off)"))

            print("\n[3/4] switching every branch ON ...")
            want, got = switch_all(crate, True, args)
            data["do_checks"].append((want, got))
            print(f"      wrote 0x{want:04X}, read back 0x{got:04X}"
                  f"{'' if want == got else '   *** MISMATCH ***'}")
            data["word_on_state"] = got     # what actually latched, not what we asked
            data["on"] = measure(crate, nodes, data["off"], sync_s,
                                 args.source, "on-state scan")
            print_measurement(data["on"], "all branches ON", args)
            n_up = sum(1 for b in range(16) for k in ("canv", "adv")
                       if classify_voltage(branch_view(data["on"], b)[k],
                                           args.v_on_min, args.v_off_max) == "up")
            print(f"      rails up: {n_up} of 32 "
                  "(empty slots can never come up, see the occupancy table)")
        else:
            print("\n[2/4] [3/4] skipped (--skip-switch-test)")
            data["on"] = data["baseline"]
            data["word_on_state"] = data["word_as_found"]

        print(f"\n[4/4] {args.samples} repeat measurements for mean and variance ...")
        # Which branches these samples were taken under, so a rail reading 0 V
        # is only called a fault when its branch was actually commanded on.
        data["word_repeats"] = crate.do_word()
        prev = data["on"] if data["on"] is not None else data["baseline"]
        data["repeats"] = []
        for i in range(args.samples):
            m = measure(crate, nodes, prev, sync_s, args.source,
                        f"sample {i + 1}/{args.samples}")
            data["repeats"].append(m)
            prev = m

        stats = reduce_samples(data["repeats"], args)
        res = analyse(data, stats, args)
        nfaults = report(res, data, stats, args)

        if args.restore_as_found and not args.skip_switch_test:
            crate.write_word(data["word_as_found"])
            time.sleep(args.settle)
            print(f"\n  restored DO word to 0x{crate.do_word():04X} (as found)")
        elif not args.skip_switch_test:
            print(f"\n  crate left with every branch ON "
                  f"(DO word 0x{crate.do_word():04X})")

        if args.json:
            write_json(args.json, data, stats, res, args, server)
            print(f"  raw samples and statistics written to {args.json}")

    if not args.use_running_server:
        print("\ndone -- server stopped")
    return 1 if nfaults else 0


def write_json(path, data, stats, res, args, server):
    def dump_meas(m):
        return None if m is None else [
            {"channel": s.channel, "uv": s.uv, "ok": s.ok,
             "timestamp": None if s.timestamp is None else str(s.timestamp)}
            for s in m]
    out = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "config_file": server.config_file, "endpoint": args.endpoint,
        "thresholds": {"v_on_min": args.v_on_min, "v_off_max": args.v_off_max,
                       "i_zero_tol": args.i_zero_tol,
                       "v_stdev_max": args.v_stdev_max,
                       "i_stdev_max": args.i_stdev_max},
        "do_checks": [{"wrote": w, "read_back": g} for w, g in data["do_checks"]],
        "word_as_found": data.get("word_as_found"),
        "word_during_repeats": data.get("word_repeats"),
        "measurements": {k: dump_meas(data.get(k))
                         for k in ("baseline", "off", "on")},
        "repeats": [dump_meas(m) for m in data["repeats"]],
        "channel_map": {str(b): branch_channels(b) for b in range(16)},
        "statistics": {str(b): stats[b] for b in range(16)},
        "slots": res["slots"], "populated": res["populated"],
        "switching": {str(b): v for b, v in res["switching"].items()},
        "stability": {f"{b}.{k}": {"verdict": v[0], "why": v[1], "own_fault": v[2]}
                      for (b, k), v in res["stability"].items()},
    }
    with open(path, "w") as fh:
        json.dump(out, fh, indent=2, default=str)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ServerError as exc:
        sys.exit(f"error: {exc}")
    except KeyboardInterrupt:
        sys.exit(130)
