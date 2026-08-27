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

The default report is a slot map and one line per module -- OK, or FAIL with
the reason. -v adds the plan, the four channel tables, the per-branch
switching detail, the statistics table and the fault list behind that verdict.

How presence is decided (docs/REFERENCE.md s4). The two input types fail
differently, and that is the whole trick:

    voltage input   100:1 divider to ground -- reads ~0 whenever no rail is
                    present, whether the branch is off or the slot is empty.
                    Useless on its own for presence.
    current input   driven by a LEM HX 05-P/SP2 Hall transducer mounted IN
                    THE MODULE, in series with the branch output. It is
                    powered by the module's housekeeping supply, NOT by the
                    switched rail, so it keeps sitting at its 2.5 V zero
                    with the branch switched off. With no module in the slot
                    nothing drives the line and it floats at a few hundred mV.

So: "current input parked at 2.5 V" == a module is in that slot and its
monitoring electronics are alive. That test is valid in the OFF state as
well as the ON state, which is why step 2 is a measurement and not just a
switch test.

The transducer is rated 5 A nominal / approx +-15 A, which is exactly the
conversion constants: 2.5 V is zero and 0.625 V of departure is 5 A. Its
whole range therefore spans 0.625 V to 4.375 V at the ADC pin, so a reading
outside that band is not a current measurement at all -- the transducer
cannot produce it. Since it lives in the module, an implausible current is
a MODULE fault; no crate-side wiring is involved in making that signal.

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
    from elmbpsu_opcua import (PsuCrate, NS_URI, branch_channels, adc_volts,
                               to_volts, to_amps)
    from asyncua.sync import Client
except ImportError as exc:
    sys.exit(f"error: cannot import required modules ({exc}).\n"
             "       activate the venv first: source .venv/bin/activate")

QUANTITIES = (("canv", "CAN V", "V"), ("cani", "CAN I", "A"),
              ("adv", "AD V", "V"), ("adi", "AD I", "A"))

# LEM HX 05-P/SP2: 2.5 V at zero current, 5 A nominal at 0.625 V of departure,
# measurable to about +-15 A. So the transducer can only ever put 0.625..4.375 V
# on the ADC pin, and a reading outside that band is not a current at all.
LEM_RANGE_A = 15.0
LEM_MIN_V = 2.5 - LEM_RANGE_A * 0.625 / 5.0     # 0.625 V
LEM_MAX_V = 2.5 + LEM_RANGE_A * 0.625 / 5.0     # 4.375 V

# The ELMB works up its channel list instead of sampling all 64 at once, and
# on this crate a channel comes round again about every 11 s: 82% of readings
# taken 2 s apart were bit-identical, i.e. only 18% of the channels had been
# re-converted (docs/REFERENCE.md s6). Every wait below is built on that -- a
# window shorter than one sweep judges channels the ADC has not revisited, and
# repeat scans closer together than one sweep are the same conversions twice.
ELMB_SWEEP_S = 12.0
SETTLE_WORD = {"steady": "stopped moving", "timeout": "NEVER SETTLED"}


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
def wait_for_new_scan(crate, nodes, prev, sync_s, source, margin=0.75, poll=0.2,
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


def rail_volts(samples):
    """The readable voltage channels of one scan, in volts, keyed (branch,
    key)."""
    out = {}
    for b in range(16):
        ch = branch_channels(b)
        for k in ("canv", "adv"):
            s = samples[ch[k]]
            if s.ok:
                out[(b, k)] = to_volts(s.uv)
    return out


def moving_rails(a, b, tol):
    """The voltage channels that moved more than tol between two scans, sorted.
    None if the two scans have no readable channel in common, which is not the
    same as nothing moving."""
    va, vb = rail_volts(a), rail_volts(b)
    common = set(va) & set(vb)
    if not common:
        return None
    return sorted(k for k in common if abs(va[k] - vb[k]) > tol)


def scan_until_stable(crate, nodes, prev, sync_s, args, label=""):
    """Keep taking fresh scans until the rails hold still. Returns
    (samples, how, scans, seconds) with how in reached/steady/timeout.

    One fresh TPDO3 set is NOT a settled measurement. The rails have their own
    rise and fall times, and the ELMB works through its 64 analog inputs at its
    own pace, so the first set to arrive after a switch can hold values sampled
    before it -- even a mix, some channels from before the switch and some from
    after. Reading it straight away is how a healthy module gets reported as
    "fails to turn on" while the repeat scans a few seconds later read 12 V.

    The rule is that no voltage channel may have moved more than
    --settle-tol over a whole --settle-window. Two scans in a row are not
    enough: back-to-back reads can both be stale and agree with each other,
    which is why the comparison is against a scan a full window old rather than
    against the previous one. The window has to outlast the ELMB's sweep, or
    "nothing moved" only means "the channels the ADC happened to revisit did
    not move" -- the rest are bit-identical because they were never
    re-converted, not because they are steady.

    Failing that, it gives up at --settle-timeout and says which rails were
    still moving, instead of handing back an unsettled scan as though it were a
    measurement.

    The scans themselves are cheap -- TPDO3.chNN.value is the server's own
    SYNC-driven cache, so reading it puts nothing on the CAN bus -- but there
    is no point taking one every SYNC when the answer cannot change until a
    whole window has passed. Four to the window is plenty, and it keeps the
    loop off the bus for real with --source sdo, where every read IS a CAN
    transfer."""
    t0 = time.monotonic()
    poll = max(1.0, sync_s, args.settle_window / 4.0)
    history = []                       # (when, samples), oldest first
    last = prev
    while True:
        started = time.monotonic()
        samples = measure(crate, nodes, last, sync_s, args.source, label,
                          quiet=True)
        spare = poll - (time.monotonic() - started)
        if spare > 0:
            time.sleep(spare)
        now = time.monotonic()
        last = samples
        older = [s for when, s in history if now - when >= args.settle_window]
        moving = moving_rails(older[-1], samples, args.settle_tol) if older else None
        if moving == []:
            return samples, "steady", len(history) + 1, now - t0, []
        history.append((now, samples))
        if now - t0 >= args.settle_timeout:
            # Which rails are still moving is a per-channel fact: one module
            # bleeding down slowly must not make every other module's verdict
            # unjudgeable.
            ref = older[-1] if older else (history[0][1] if history else None)
            still = moving_rails(ref, samples, args.settle_tol) if ref else None
            return (samples, "timeout", len(history), now - t0,
                    still if still is not None else sorted(rail_volts(samples)))


def duplicate_fraction(series):
    """How much of a repeat set is the same ADC sample counted twice. TPDO3 is
    a cache: read it faster than the ELMB refreshes a channel and consecutive
    reads come back bit-identical, which would make the variance a fiction."""
    pairs = same = 0
    for a, b in zip(series, series[1:]):
        for x, y in zip(a, b):
            if x.ok and y.ok:
                pairs += 1
                same += (x.uv == y.uv)
    return (same / pairs) if pairs else 0.0


def branch_view(samples, branch):
    """The four samples belonging to one branch, keyed canv/adv/cani/adi."""
    return {k: samples[c] for k, c in branch_channels(branch).items()}


# ------------------------------------------------------- classification
def classify_current(sample, tol):
    """What a current-sense input is telling us, judged at the ADC pin rather
    than after conversion -- see the module docstring.

    Outside LEM_MIN_V..LEM_MAX_V the transducer physically cannot be the
    source, whatever the number converts to, so that is "undriven" on the
    datasheet rather than on a tolerance we picked."""
    if not sample.ok:
        return "unreadable"
    v = adc_volts(sample.uv)
    if abs(v - 2.5) <= tol:
        return "zero"          # transducer present, powered, carrying ~no current
    if v < LEM_MIN_V or v > LEM_MAX_V:
        return "undriven"      # beyond +-15 A: nothing is driving this line
    if v < 2.5 - tol:
        return "undriven"      # below the zero on an unloaded rail
    return "implausible"       # above the zero reference on an unloaded rail


def classify_voltage(sample, on_min, off_max):
    """down / partial / up. Only "down" counts as switched off: a rail sitting
    at some intermediate voltage has switched ON, it is just not healthy, and
    that is judged separately (abnormal output, see analyse())."""
    if not sample.ok:
        return "unreadable"
    v = to_volts(sample.uv)
    if v <= off_max:
        return "down"
    if v >= on_min:
        return "up"
    return "partial"           # on, but not a healthy rail


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
# Every fault ends up in one of these buckets, and the default report prints
# nothing but the bucket names -- which is the whole point: "module 3 is bad
# and here is the one-line reason". The detail behind each lives in -v.
FAULT_TEXT = {
    "command":     "the crate never took the command",
    "onoff_on":    "fails to turn on",
    "onoff_off":   "fails to turn off",
    "onoff_other": "on/off test inconclusive",
    "sensor":      "current sensor does not work",
    "current":     "unexpected current",
    "unsettled":   "on/off not judged, the rail was still moving",
    "voltage":     "abnormal output voltage",
    "unstable":    "unsteady reading",
    "unreadable":  "channel not readable",
    "other":       "module not fully accounted for",
}
LABEL = dict((k, l) for k, l, _ in QUANTITIES)
QORDER = dict((k, i) for i, (k, _, _) in enumerate(QUANTITIES))


def _chan_sort(item):
    """(branch, key) in reading order -- CAN V, CAN I, AD V, AD I -- rather
    than in the alphabetical order of the dict keys."""
    (b, key) = item[0] if isinstance(item[0], tuple) else item
    return (b, QORDER[key])


def _volts(v):
    """A voltage for the report, with -0.00 normalised away so two readings
    of the same nothing print as the same string."""
    return f"{0.0 if abs(v) < 5e-3 else v:.2f} V"


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
                           if st == "on" and v in ("up", "partial")})

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
                      "not driven -- transducer or its connection in this module")
        elif len(alive) == 4 and len(rails_up) < n_expect:
            verdict = "POPULATED*"
            detail = (f"sensors all alive but only {len(rails_up)} of {n_expect} "
                      "commanded rails came up -- output stage or rail fault")
        elif not alive and rails_up:
            verdict = "POPULATED*"
            detail = ("rails are up but no transducer holds its zero -- "
                      "contradictory, suspect this module's sense electronics")
        else:
            verdict, detail = "POPULATED*", "mixed evidence, see the channel table"
        slots[s] = {"branches": list(branches), "verdict": verdict,
                    "detail": detail, "sensors_alive": len(alive),
                    "rails_up_on": len(rails_up), "rails_expected": n_expect,
                    "undriven": [f"branch {b} {k}" for b, k in undriven]}

    populated = [s for s in range(8) if slots[s]["verdict"] != "ABSENT"]

    # --- switching, per branch of a populated slot ------------------------
    # "Off" is only the rail near zero; any voltage above that is the branch
    # having switched on, healthy or not. A rail that comes up at 6 V passes
    # here and is caught by the abnormal-output test instead, so the two
    # failures stay distinguishable in the report.
    switching = {}
    if off is not None and on is not None:
        for s in populated:
            for b in slots[s]["branches"]:
                vo, vn = branch_view(off, b), branch_view(on, b)
                rows = {}
                for key in ("canv", "adv"):
                    down = classify_voltage(vo[key], args.v_on_min, args.v_off_max)
                    up = classify_voltage(vn[key], args.v_on_min, args.v_off_max)
                    if "unreadable" in (down, up):
                        r, cat = f"UNCLEAR (off={down}, on={up})", "onoff_other"
                    elif down == "down" and up != "down":
                        r, cat = "OK", None
                    elif down != "down" and up != "down":
                        r, cat = "STUCK ON (did not drop when switched off)", "onoff_off"
                    elif down == "down" and up == "down":
                        r, cat = "NO OUTPUT (did not come up when switched on)", "onoff_on"
                    else:
                        r, cat = ("INVERTED (up when commanded off, down when "
                                  "commanded on -- wrong polarity?)", "onoff_other")
                    rows[key] = {"result": r, "category": cat,
                                 "off_v": to_volts(vo[key].uv) if vo[key].ok else None,
                                 "on_v": to_volts(vn[key].uv) if vn[key].ok else None}
                switching[b] = rows

    # --- did the measurements settle? -------------------------------------
    # A scan taken while the rails were still moving is not evidence. Two
    # things can show that after the fact: the settle wait gave up while values
    # were still changing, or the on-state scan and the repeat scans disagree
    # about whether a rail is up at all -- the same rail, minutes apart, cannot
    # be both. Either way the honest answer is "not judged", not two
    # contradictory faults against one module.
    moving = {ph: {tuple(x) for x in data.get("moving", {}).get(ph, [])}
              for ph in ("off", "on")}
    for b, rows in switching.items():
        for key in ("canv", "adv"):
            r, st = rows[key], stats[b][key]
            why = None
            if r["category"] == "onoff_off" and (b, key) in moving["off"]:
                why = (f"the off-state scan read {r['off_v']:.2f} V but this "
                       "rail never stopped changing, so it may simply have "
                       "been on its way down")
            elif r["category"] == "onoff_on" and (b, key) in moving["on"]:
                why = (f"the on-state scan read {r['on_v']:.2f} V but this "
                       "rail never stopped changing, so it may simply not "
                       "have been up yet")
            if (st is not None and r["on_v"] is not None
                    and (r["on_v"] <= args.v_off_max)
                    != (st["mean"] <= args.v_off_max)):
                why = (f"the on-state scan read {r['on_v']:.2f} V and the "
                       f"repeat scans {st['mean']:.2f} V -- one rail cannot be "
                       "both")
            if why:
                r["result"] = f"NOT SETTLED ({why})"
                r["category"] = "unsettled"

    # --- sensor health, then stability ------------------------------------
    # Order matters. A sense line that nothing drives sits rock steady at a
    # few hundred mV, so it sails through any stdev test -- "steady" is not
    # "working". Plausibility is checked first, and only a channel that is
    # reporting something real gets judged on how steadily it reports it.
    # Entries are (verdict, why, is_its_own_fault, category). The third flag
    # keeps a rail the switching section already condemned from being counted
    # a second time -- it is one defect seen from two angles.
    commanded = data.get("word_repeats", 0xFFFF if on_value else 0x0000)
    stability = {}
    for s in populated:
        for b in slots[s]["branches"]:
            branch_on = ((commanded >> b) & 1) == on_value
            for key, lbl, unit in QUANTITIES:
                st = stats[b][key]
                if st is None:
                    stability[(b, key)] = ("FAIL", "never readable", True,
                                           "unreadable")
                    continue
                if st["dropped"]:
                    stability[(b, key)] = (
                        "FAIL", f"{st['dropped']} of {len(data['repeats'])} reads "
                        "came back bad", True, "unreadable")
                    continue
                if unit == "A" and abs(st["adc_mean"] - 2.5) > args.i_zero_tol:
                    adc = st["adc_mean"]
                    if adc < LEM_MIN_V or adc > LEM_MAX_V:
                        # Physically impossible for the transducer to produce,
                        # so it is not a current at all -- the line is floating.
                        stability[(b, key)] = (
                            "FAIL",
                            f"sense line reads {adc:.3f} V at the ADC pin, not "
                            "the transducer's 2.5 V zero, and outside its own "
                            f"{LEM_MIN_V:.3f}-{LEM_MAX_V:.3f} V range -- "
                            f"nothing is driving it ({st['mean']:.3f} A is what "
                            "that converts to, not a real current)",
                            True, "sensor")
                        continue
                    # In range: the transducer IS reporting this. It is a fault
                    # only because nothing should be drawing current on an
                    # unloaded crate -- with a load connected it is the load.
                    stability[(b, key)] = (
                        "FAIL", f"{st['mean']:.3f} A flowing "
                        f"(sense line at {adc:.3f} V, p-p {st['ptp']:.3f} A) -- "
                        "a real current if something is connected to this "
                        "branch, a biased sensor if not", True, "current")
                    continue
                if unit == "V" and branch_on and st["mean"] <= args.v_off_max:
                    already = switching.get(b, {}).get(key, {}).get("result", "OK")
                    stability[(b, key)] = (
                        "FAIL", f"branch is commanded ON but the rail reads "
                        f"{st['mean']:.3f} V"
                        + ("" if already == "OK" else " -- same defect as the "
                           "switching section reports"), already == "OK",
                        "onoff_on")
                    continue
                if (unit == "V" and branch_on
                        and abs(st["mean"] - args.v_nominal) > args.v_tol):
                    moving = " and still moving" if st["ptp"] > 0.5 else ""
                    stability[(b, key)] = (
                        "FAIL", f"rail reads {st['mean']:.3f} V, outside "
                        f"{args.v_nominal} +/- {args.v_tol} V (p-p "
                        f"{st['ptp']:.3f} V over {st['n']} samples{moving})",
                        True, "voltage")
                    continue
                limit = args.v_stdev_max if unit == "V" else args.i_stdev_max
                if st["n"] < 2:
                    stability[(b, key)] = ("SKIP", "needs at least 2 samples",
                                           False, "")
                elif st["stdev"] > limit:
                    stability[(b, key)] = (
                        "FAIL", f"stdev {st['stdev']:.3f}{unit} > {limit}{unit} "
                        f"(var {st['variance']:.4f}, p-p {st['ptp']:.3f}{unit}, "
                        f"{st['min']:.3f} .. {st['max']:.3f}{unit})", True,
                        "unstable")
                else:
                    stability[(b, key)] = ("PASS", f"stdev {st['stdev']:.3f}{unit}",
                                           False, "")

    res = {"slots": slots, "populated": populated,
           "switching": switching, "stability": stability,
           "warnings": settle_warnings(data, args, switching)}
    res["faults"] = collect_faults(res, data, stats)
    res["modules"] = summarise_modules(res)
    return res


def settle_warnings(data, args, switching):
    """What the run itself could not measure cleanly, in plain words. These are
    not module faults -- they are reasons to distrust a verdict.

    Rails that were still moving are only worth a line if that actually cost a
    verdict. A rail still drifting when the wait ended, but whose on/off answer
    came out unambiguous anyway, is not something to report."""
    out = []
    cost = {k for b, rows in switching.items() for k, r in rows.items()
            if r["category"] == "unsettled"}
    for phase in ("off", "on"):
        still = [x for x in (data.get("moving", {}).get(phase) or [])
                 if tuple(x) in {(b, k) for b, rows in switching.items()
                                 for k in rows}]
        if still and cost:
            named = ", ".join(f"branch {b} {LABEL[k]}" for b, k in still[:6])
            out.append(f"{len(still)} rail(s) still moving "
                       f"{args.settle_timeout:.0f}s after switching "
                       f"{phase.upper()} ({named}"
                       f"{', ...' if len(still) > 6 else ''}) -- re-run, or "
                       "raise --settle-timeout")
    dup = data.get("duplicate_fraction", 0.0)
    if dup > 0.5:
        want = data.get("sample_interval", 0.0) / max(1.0 - dup, 0.01)
        out.append(f"{dup * 100:.0f}% of consecutive repeat readings were "
                   "bit-identical, so the repeat scans are largely the same "
                   "conversions read twice and the variance is understated -- "
                   f"try --sample-interval {want:.0f}")
    return out


def collect_faults(res, data, stats):
    """One flat list of findings, each tied to the slot -- i.e. the module --
    it condemns, or to None for a crate/server-level one."""
    faults = []
    for want, got in data["do_checks"]:
        if want != got:
            faults.append({
                "slot": None, "category": "command", "item": "DO word",
                "branch": None, "label": "",
                "value": f"read back 0x{got:04X}, wrote 0x{want:04X}",
                "text": f"DO read-back 0x{got:04X} != 0x{want:04X} -- the "
                        "command never reached the outputs"})
    for b, rows in sorted(res["switching"].items()):
        for key in ("canv", "adv"):
            r = rows[key]
            if not r["category"]:
                continue
            v = "n/a" if r["on_v"] is None else _volts(r["on_v"])
            faults.append({
                "slot": b // 2, "category": r["category"],
                "item": f"branch {b} {LABEL[key]}", "value": v,
                "branch": b, "label": LABEL[key].strip(),
                "text": f"branch {b} {LABEL[key]}: {r['result']}"})
    for (b, key), (verdict, why, own, cat) in sorted(res["stability"].items(),
                                                     key=_chan_sort):
        if verdict != "FAIL" or not own:
            continue
        st = stats[b][key]
        unit = dict((k, u) for k, _, u in QUANTITIES)[key]
        if st is None:
            v = "n/a"
        elif unit == "A":
            v = f"{st['adc_mean']:.2f} V at the ADC pin"
        else:
            v = _volts(st["mean"])
        faults.append({
            "slot": b // 2, "category": cat,
            "item": f"branch {b} {LABEL[key]}", "value": v,
            "branch": b, "label": LABEL[key].strip(),
            "text": f"branch {b} {LABEL[key]}: {why}"})
    # One unreadable channel is one defect: if the stability pass named it,
    # drop the switching entry, which could only say the same thing again.
    dead = {f["item"] for f in faults if f["category"] == "unreadable"}
    faults = [f for f in faults
              if not (f["category"] == "onoff_other" and f["item"] in dead)]
    # Slot-level findings only where no channel-level fault already names the
    # slot, so a partially-seated module is not reported three times over.
    named = {f["slot"] for f in faults}
    for s in range(8):
        if res["slots"][s]["verdict"] == "POPULATED*" and s not in named:
            faults.append({"slot": s, "category": "other", "item": f"slot {s}",
                           "value": "", "branch": None, "label": "",
                           "text": res["slots"][s]["detail"]})
    return faults


def summarise_modules(res):
    """Per populated slot: OK, or FAIL plus the distinct reasons, in the order
    they were found. This is what the default report prints."""
    modules = {}
    for s in res["populated"]:
        groups = []
        for f in res["faults"]:
            if f["slot"] != s:
                continue
            for g in groups:
                if g["category"] == f["category"]:
                    break
            else:
                g = {"category": f["category"],
                     "text": FAULT_TEXT.get(f["category"], f["category"]),
                     "items": [], "values": [], "branches": [], "labels": []}
                groups.append(g)
            if f["item"] not in g["items"]:
                g["items"].append(f["item"])
                g["values"].append(f["value"])
                g["branches"].append(f["branch"])
                g["labels"].append(f["label"])
        # A rail whose on/off could not be judged is not a verdict either
        # way: the module is UNKNOWN, not condemned, unless something else
        # about it really did fail.
        real = [g for g in groups if g["category"] != "unsettled"]
        modules[s] = {"slot": s,
                      "status": "FAIL" if real else "UNKNOWN" if groups else "OK",
                      "faults": groups,
                      "summary": "; ".join(fault_line(g) for g in groups) or "OK"}
    return modules


def fault_line(g):
    """One reason, as the default report prints it. Channels reading the same
    thing share one value instead of repeating it, and branches that failed on
    the same channels collapse together -- so a module whose four rails are
    all dead is one short phrase, not four."""
    vals = [v for v in g["values"] if v]
    value = f" = {vals[0]}" if vals and len(set(vals)) == 1 else ""
    per_branch = {}
    for b, lbl in zip(g["branches"], g["labels"]):
        per_branch.setdefault(b, []).append(lbl)
    if (value and None not in per_branch
            and len(set(tuple(v) for v in per_branch.values())) == 1):
        word = "branch" if len(per_branch) == 1 else "branches"
        where = (f"{word} {', '.join(str(b) for b in per_branch)} "
                 + "+".join(next(iter(per_branch.values()))))
    elif value:
        where = ", ".join(g["items"])
    else:
        where = ", ".join(i + (f" = {v}" if v else "")
                          for i, v in zip(g["items"], g["values"]))
    return f"{g['text']}: {where}{value}"


# --------------------------------------------------------------- reports
def report(res, data, args):
    """Print the findings and return the number of faults."""
    print("\nslots (one module = two branches: slot s owns 2s and 2s+1)")
    for s in range(8):
        print(f"  [{s}]" + ("  <-- populated" if s in res["populated"] else ""))

    print("\nmodules")
    for f in res["faults"]:
        if f["slot"] is None:
            print(f"  Crate : FAIL ({FAULT_TEXT[f['category']]}: {f['value']})")
    for s in res["populated"]:
        m = res["modules"][s]
        if m["status"] == "OK":
            print(f"  Module {s}: OK")
        else:
            print(f"  Module {s}: {m['status']} ({m['summary']})")

    if data["off"] is None:
        print("  (on/off was not tested -- --skip-switch-test)")
    for w in res["warnings"]:
        print(f"  ! {w}")

    bad = sorted(s for s in res["populated"]
                 if res["modules"][s]["status"] == "FAIL")
    unknown = sorted(s for s in res["populated"]
                     if res["modules"][s]["status"] == "UNKNOWN")
    n = len(res["populated"])
    if not n:
        print("  none detected -- no slot drives its sense lines")
    head = f"\n{n} module{'' if n == 1 else 's'} present, "
    tail = "" if args.verbose else "   (-v for the measurements)"
    parts = []
    if bad:
        parts.append(f"{len(bad)} faulty: replace {_mods_str(bad)}")
    if unknown:
        parts.append(f"{len(unknown)} not judged ({_mods_str(unknown)}) -- "
                     "re-run, and see the note above")
    if not parts:
        parts.append("none faulty -- but the crate itself failed, above"
                     if res["faults"] else "all OK.")
    print(head + "; ".join(parts) + tail)
    return len(bad), len(unknown)


def report_verbose(res, data, stats, args):
    """Everything the run measured, printed before the summary."""
    slots, populated = res["slots"], res["populated"]

    print("\n=== SLOT OCCUPANCY ===")
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
        for phase, how in sorted(data.get("settle", {}).items()):
            print(f"  after switching {phase.upper():3}: rails "
                  f"{SETTLE_WORD[how]}")
        for want, got in data["do_checks"]:
            ok = "OK" if want == got else "*** MISMATCH ***"
            print(f"  DO word: wrote 0x{want:04X}, read back 0x{got:04X}   {ok}")
        print(f"\n  physical response, populated slots only. <= {args.v_off_max} V "
              "is off; anything above it")
        print("  has switched on, healthy or not (an abnormal level is a "
              "separate finding below):")
        for b in sorted(res["switching"]):
            for key in ("canv", "adv"):
                r = res["switching"][b][key]
                off_v = "n/a" if r["off_v"] is None else f"{r['off_v']:7.3f}V"
                on_v = "n/a" if r["on_v"] is None else f"{r['on_v']:7.3f}V"
                mark = "" if r["result"] == "OK" else "   <<<"
                print(f"    branch {b:>2} {LABEL[key]:>5}:  off {off_v}   "
                      f"on {on_v}   {r['result']}{mark}")

    print(f"\n=== SENSORS ({len(data['repeats'])} samples) ===")
    print("  a channel passes only if it reports something plausible AND does "
          "so steadily:")
    print(f"    plausible  current input within {args.i_zero_tol} V of its 2.5 V "
          "zero at the ADC pin;")
    print(f"               voltage input {args.v_nominal} +/- {args.v_tol} V when "
          "its branch is commanded on")
    print(f"    steady     stdev <= {args.v_stdev_max} V / {args.i_stdev_max} A. "
          "variance = stdev^2 and is in the JSON.\n")
    print_stats(stats, args, only=set(
        b for s in populated for b in slots[s]["branches"]))
    npass = sum(1 for v in res["stability"].values() if v[0] == "PASS")
    nfail = sum(1 for v in res["stability"].values() if v[0] == "FAIL")
    print(f"\n  {npass} pass, {nfail} fail, of "
          f"{len(res['stability'])} channels on populated slots")
    for (b, key), (verdict, why, own, cat) in sorted(res["stability"].items(),
                                                     key=_chan_sort):
        if verdict != "PASS":
            print(f"    {verdict}  branch {b:>2} {LABEL[key]:>5}: {why}")

    print("\n=== FAULTS ===")
    if not res["faults"]:
        print("  none.")
    else:
        for f in res["faults"]:
            where = "crate/server" if f["slot"] is None else \
                f"slot {f['slot']} (branches {2 * f['slot']} and {2 * f['slot'] + 1})"
            print(f"  - {where}: {f['text']}")
        _print_interpretation(res)


def _mods_str(slots):
    return ", ".join(f"module {s}" for s in slots) if slots else "none"


def _print_interpretation(res):
    """What a fault here means. The current transducer (LEM HX 05-P/SP2) is
    mounted in the module and the voltage divider is on the module too, so
    nothing the crate does can produce or fix a bad reading -- see
    docs/REFERENCE.md s4."""
    suspect = sorted({f["slot"] for f in res["faults"] if f["slot"] is not None})
    sense_faults = [s for s in suspect if res["slots"][s]["undriven"]]
    print("\n  The sense electronics -- the 100:1 divider and the LEM "
          "HX 05-P/SP2 current")
    print("  transducer -- are inside the MODULE, so a fault named above is a "
          "module fault,")
    print("  not a crate one.")
    if sense_faults:
        plural = "s" if len(sense_faults) > 1 else ""
        print(f"    Slot{plural} {', '.join(str(s) for s in sense_faults)} "
              f"ha{'ve' if plural else 's'} sense lines that nothing drives while")
        print("    other channels of the same module are fine. A module that was "
              "simply dead")
        print("    could not produce the good channels, so the transducer or its "
              "connection")
        print("    inside that module has failed.")
        print("      1. reseat the module once, to rule out a partly-made "
              "connector, and re-run.")
        print("      2. if it persists, replace the module.")
    elif suspect:
        print("    No undriven sense lines: every module that is present is "
              "reporting.")
        print("    Faults listed above are rail/output or stability problems in "
              "the")
        print("    module's own converter.")


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
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print every channel table, the per-branch switching "
                        "detail and the statistics (default: slot map and one "
                        "line per module)")
    p.add_argument("--settle", type=float, default=1.0,
                   help="seconds to wait after a switch before the read-back "
                        "(default 1)")
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
                   help="write the per-channel statistics and findings here")

    g = p.add_argument_group("settling")
    g.add_argument("--settle-window", type=float, default=ELMB_SWEEP_S,
                   help="a switched rail counts as settled once no voltage "
                        "channel has moved for this long (default "
                        "%(default)s s = one ELMB sweep; shorter and a stale "
                        "reading looks settled)")
    g.add_argument("--settle-tol", type=float, default=0.2,
                   help="volts of movement still counted as holding still "
                        "(default %(default)s)")
    g.add_argument("--settle-timeout", type=float, default=4 * ELMB_SWEEP_S,
                   help="give up waiting for the rails after this and mark "
                        "those on/off verdicts unjudged (default %(default)s s)")
    g.add_argument("--sample-interval", type=float, default=ELMB_SWEEP_S,
                   help="seconds between the repeat scans (default "
                        "%(default)s s = one ELMB sweep). Any closer and they "
                        "are the same conversions read twice, and the variance "
                        "means nothing.")

    g = p.add_argument_group("pass/fail thresholds")
    g.add_argument("--v-nominal", type=float, default=12.0,
                   help="expected rail voltage (default 12.0)")
    g.add_argument("--v-tol", type=float, default=0.9,
                   help="a commanded-on rail further than this from --v-nominal "
                        "is an abnormal output (default 0.9 V)")
    g.add_argument("--v-on-min", type=float, default=None,
                   help="rail volts at or above this are a healthy rail "
                        "(default: --v-nominal minus --v-tol)")
    g.add_argument("--v-off-max", type=float, default=0.5,
                   help="rail volts at or below this count as OFF; anything "
                        "above it has switched on, healthy or not (default 0.5)")
    g.add_argument("--i-zero-tol", type=float, default=0.1,
                   help="volts at the ADC pin either side of the sensor's 2.5 V "
                        "zero still counted as 'sensor alive' (default 0.1, "
                        "i.e. +/-0.8 A). Assumes nothing is drawing real current.")
    g.add_argument("--v-stdev-max", type=float, default=0.03,
                   help="stability limit for voltage channels (default 0.03 V)")
    g.add_argument("--i-stdev-max", type=float, default=0.03,
                   help="stability limit for current channels (default 0.03 A)")

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

    if args.v_on_min is None:
        args.v_on_min = args.v_nominal - args.v_tol

    if args.samples < 1:
        # Zero repeats leaves every channel with no statistics at all, which
        # the sensor checks would read as "never readable" and condemn.
        args.samples = 1
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
    # The repeat scans cost one ELMB sweep each -- that is the floor, not a
    # setting -- and each switch costs a settle window on top.
    est = (nodeguard_s + sync_s + args.samples * args.sample_interval
           + (0 if args.skip_switch_test
              else 2 * (args.settle + args.settle_window + 2 * sync_s)))
    print(f"crate scan: {n_scans}+ scans of all 64 channels at one SYNC "
          f"({sync_s * 1000:.0f} ms) each, ~{est:.0f}s plus however long the "
          "rails take to settle"
          + ("" if args.skip_switch_test
             else "\n            every branch is switched OFF and back ON"))
    if sync_s >= 5.0:
        print(f"            every wait below is rounded up to a whole SYNC, so "
              f"setting syncIntervalMs to 1000 in "
              f"{Path(server.config_file).name}\n            (and restarting "
              "the server) takes a large bite out of that")

    with contextlib.ExitStack() as stack:
        if args.use_running_server:
            if not server.is_running():
                sys.exit("error: --use-running-server but no CanOpenOpcUa is "
                         f"running for {server.config_file}")
            print(f"  attached to the running server, pid {server.pid()}")
        else:
            # OpcUaServer.start() refuses if one is already running for this
            # config file, which is what keeps two CANopen masters off the bus.
            stack.enter_context(server)
            if not server.wait_ready(timeout=args.start_timeout):
                sys.exit("error: OPC-UA endpoint never opened -- check server.log")
            print(f"  server started, pid {server.pid()}")

        client = Client(url=args.endpoint)
        client.connect()
        stack.callback(client.disconnect)
        try:
            ns = client.get_namespace_index(NS_URI)
        except Exception:
            ns = 2
        crate = PsuCrate(client, ns, args.bus, args.node)
        nodes = crate.ai_nodes(args.source)

        # The endpoint being open is not the crate answering, and the crate
        # answering is not the crate being ready. A crate that has just been
        # power-cycled boots into PRE-OPERATIONAL and the server only drives
        # it to Node/@requestedState on a node-management cycle, so the first
        # readable state is usually the wrong one. Waiting for the state we
        # actually need is what makes the first run after a power cycle work
        # rather than exit and ask to be re-run.
        want = "OPERATIONAL" if args.method == "rpdo" else None
        print(f"  waiting up to {args.warmup:.0f}s for the crate"
              f"{' to reach OPERATIONAL' if want else ' to answer'} "
              f"(node guard every {nodeguard_s:.0f}s) ...")
        ok, state = crate.wait_ready(timeout=args.warmup, require=want)
        if not ok:
            answered, now = crate.ping()
            if answered:
                sys.exit(f"error: node is {now} after {args.warmup:.0f}s, not "
                         "OPERATIONAL, and RPDO writes are only acted on in "
                         "OPERATIONAL.\n       The server drives the node to "
                         "Node/@requestedState -- check that it says "
                         "OPERATIONAL in\n       the config, raise --warmup, or "
                         "use --method sdo, which works in PRE-OPERATIONAL.")
            sys.exit(f"error: the crate never answered: {state}\n"
                     "       check --bus/--node against config-elmbpsu.xml, and "
                     "server.log for the node table")
        print(f"  crate {state}, DO word 0x{crate.do_word():04X}")

        data = {"do_checks": [], "off": None, "on": None}

        print("\n  [1/4] measuring as found ...")
        data["word_as_found"] = crate.do_word()
        # First scan gets a longer budget: the TPDO3 cache is empty until the
        # server's first SYNC, which can be a whole syncIntervalMs away.
        data["baseline"] = measure(crate, nodes, None, sync_s, args.source,
                                   "baseline", quiet=not args.verbose,
                                   timeout=max(args.warmup, sync_s + 5.0))
        if args.verbose:
            print_measurement(data["baseline"], "as found", args)


        data["settle"], data["moving"] = {}, {}
        if not args.skip_switch_test:
            want_w, got = switch_all(crate, False, args)
            data["do_checks"].append((want_w, got))
            data["off"], how, ns, secs, moving = scan_until_stable(
                crate, nodes, data["baseline"], sync_s, args, label="off-state")
            data["settle"]["off"], data["moving"]["off"] = how, moving
            print(f"  [2/4] all branches OFF: wrote 0x{want_w:04X}, read back "
                  f"0x{got:04X}"
                  f"{'' if want_w == got else '   *** MISMATCH ***'}"
                  f" -- rails {SETTLE_WORD[how]} after {secs:.0f}s"
                  + (f", {ns} cache reads" if args.verbose else ""))
            if args.verbose:
                print_measurement(data["off"], "all branches OFF", args)
                n_up = sum(1 for b in range(16) for k in ("canv", "adv")
                           if classify_voltage(branch_view(data["off"], b)[k],
                                               args.v_on_min,
                                               args.v_off_max) != "down")
                print(f"      rails still up: {n_up}"
                      + ("  <- these did not switch off" if n_up else "  (all off)"))

            want_w, got = switch_all(crate, True, args)
            data["do_checks"].append((want_w, got))
            data["word_on_state"] = got     # what actually latched, not what we asked
            data["on"], how, ns, secs, moving = scan_until_stable(
                crate, nodes, data["off"], sync_s, args, label="on-state")
            data["settle"]["on"], data["moving"]["on"] = how, moving
            print(f"  [3/4] all branches ON : wrote 0x{want_w:04X}, read back "
                  f"0x{got:04X}"
                  f"{'' if want_w == got else '   *** MISMATCH ***'}"
                  f" -- rails {SETTLE_WORD[how]} after {secs:.0f}s"
                  + (f", {ns} cache reads" if args.verbose else ""))
            if args.verbose:
                print_measurement(data["on"], "all branches ON", args)
                n_up = sum(1 for b in range(16) for k in ("canv", "adv")
                           if classify_voltage(branch_view(data["on"], b)[k],
                                               args.v_on_min,
                                               args.v_off_max) != "down")
                print(f"      rails up: {n_up} of 32 "
                      "(empty slots can never come up, see the occupancy table)")
        else:
            print("  [2/4] [3/4] skipped (--skip-switch-test)")
            data["on"] = data["baseline"]
            data["word_on_state"] = data["word_as_found"]

        print(f"  [4/4] {args.samples} repeat scans {args.sample_interval:.0f}s "
              f"apart for mean and variance, "
              f"~{args.samples * args.sample_interval:.0f}s ...")
        # Which branches these samples were taken under, so a rail reading 0 V
        # is only called a fault when its branch was actually commanded on.
        data["word_repeats"] = crate.do_word()
        prev = data["on"] if data["on"] is not None else data["baseline"]
        data["repeats"] = []
        taken = 0.0
        for i in range(args.samples):
            # Spaced deliberately: a fresh TPDO3 set is not a fresh ADC sample,
            # and reading the cache faster than the ELMB refreshes it would
            # give a variance of nearly zero that means nothing.
            wait = args.sample_interval - (time.monotonic() - taken)
            if i and wait > 0:
                time.sleep(wait)
            taken = time.monotonic()
            m = measure(crate, nodes, prev, sync_s, args.source,
                        f"sample {i + 1}/{args.samples}", quiet=not args.verbose)
            data["repeats"].append(m)
            prev = m
        data["duplicate_fraction"] = duplicate_fraction(data["repeats"])
        data["sample_interval"] = args.sample_interval

        stats = reduce_samples(data["repeats"], args)
        res = analyse(data, stats, args)
        if args.verbose:
            report_verbose(res, data, stats, args)
        nfail, nunknown = report(res, data, args)

        if args.restore_as_found and not args.skip_switch_test:
            crate.write_word(data["word_as_found"])
            time.sleep(args.settle)
            print(f"\ncrate DO word restored to 0x{crate.do_word():04X} (as found)")
        elif not args.skip_switch_test:
            print(f"\ncrate left with every branch ON "
                  f"(DO word 0x{crate.do_word():04X})")

        if args.json:
            write_json(args.json, data, stats, res, args, server)
            print(f"statistics and findings written to {args.json}")

    # 0 nothing found, 1 something is faulty, 2 nothing faulty but something
    # could not be judged -- which is not a pass.
    return 1 if nfail else (2 if nunknown else 0)


def write_json(path, data, stats, res, args, server):
    """Statistics and findings, not raw samples. Every scan of every channel
    with its SourceTimestamp was most of the file's size and none of its
    value: the mean and variance are what every verdict is made of, and the
    single-shot states are kept only as the engineering values the on/off
    verdicts rest on."""
    conv = dict((k, to_volts if u == "V" else to_amps) for k, _, u in QUANTITIES)

    def state_values(m):
        if m is None:
            return None
        return {str(b): {k: (None if not s.ok else round(conv[k](s.uv), 4))
                         for k, s in branch_view(m, b).items()}
                for b in range(16)}

    def stat_values(st):
        if st is None:
            return None
        return {k: (round(st[k], 6) if isinstance(st[k], float) else st[k])
                for k in ("n", "dropped", "mean", "variance", "stdev", "adc_mean")}

    out = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "config_file": server.config_file, "endpoint": args.endpoint,
        "samples": len(data["repeats"]),
        "thresholds": {"v_nominal": args.v_nominal, "v_tol": args.v_tol,
                       "v_on_min": args.v_on_min, "v_off_max": args.v_off_max,
                       "i_zero_tol": args.i_zero_tol,
                       "v_stdev_max": args.v_stdev_max,
                       "i_stdev_max": args.i_stdev_max},
        "do_checks": [{"wrote": w, "read_back": g} for w, g in data["do_checks"]],
        "settled": data.get("settle", {}),
        "still_moving": {ph: [f"{b}.{k}" for b, k in v]
                         for ph, v in data.get("moving", {}).items()},
        "duplicate_fraction": round(data.get("duplicate_fraction", 0.0), 4),
        "sample_interval_s": data.get("sample_interval"),
        "warnings": res["warnings"],
        "word_as_found": data.get("word_as_found"),
        "word_during_repeats": data.get("word_repeats"),
        "channel_map": {str(b): branch_channels(b) for b in range(16)},
        "states": {k: state_values(data.get(k))
                   for k in ("baseline", "off", "on")},
        "statistics": {str(b): {k: stat_values(stats[b][k])
                                for k, _, _ in QUANTITIES} for b in range(16)},
        "slots": res["slots"], "populated": res["populated"],
        "modules": {str(s): m for s, m in res["modules"].items()},
        "faults": res["faults"],
        "switching": {str(b): v for b, v in res["switching"].items()},
        "stability": {f"{b}.{k}": {"verdict": v[0], "why": v[1],
                                   "own_fault": v[2], "category": v[3]}
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
