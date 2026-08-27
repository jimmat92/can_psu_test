# ELMB PSU project — handoff

Live state for an agent or colleague picking this up cold: the task, what the
crate and the machine are doing right now, what was corrected along the way, and
what is still open.

**Settled protocol, mapping and server knowledge is in
[REFERENCE.md](REFERENCE.md)**, not repeated here. Operator procedure:
[QUICKSTART.md](QUICKSTART.md).

---

## 1. The task and its constraints

The user has a **CERN ELMB Power Supply Unit (PSU) crate** on the bench and wants
to test and operate it, explicitly **without** WinCC OA + fwElmbPSU + fwElmb. The
plan: run the CERN **CanOpenOpcUa** server against the crate's internal control
ELMB, write a **custom OPC-UA client** to replace WinCC OA, and produce a correct
**XML config** for the server. Standing constraints:

- `fwElmb/`, `fwElmbPSU/` and `CanOpenOpcUa/` are **reference material only** —
  reverse-engineer from them, never install or run them. Cite the source file for
  any protocol claim.
- **Never take a CAN bus another user owns.** `can9` belongs to a production lab
  temperature monitor. Run `./tests/can_diag.py` before touching anything.
- **Never run two CANopen masters on one bus.** While the OPC-UA server is up,
  only `scan` and `dump` are safe from `lib/elmbpsu_can.py`.
- The three reference repos are **pinned to the exact commits that were read**
  (the hashes are in `setup.sh`), because REFERENCE.md cites specific files and
  an upstream change would silently invalidate a citation. `--latest` takes
  master instead, at that risk.

---

## 2. Status

| | state |
|---|---|
| protocol, mappings, conversions | **verified** — against the JCOP sources, `tests/selftest.py` (37 checks), and against the crate |
| `lib/elmbpsu_can.py` | written, self-tested; `dump` verified on real hardware |
| `lib/elmbpsu_opcua.py` | **verified end-to-end against the real crate** on 2026-08-25 |
| `config/config-elmbpsu.xml` | **verified against the real crate** — server comes up, node table populates |
| `tests/can_diag.py` | verified both ways: detected a live server, correctly rejected sshd/cups/postfix |
| `tests/crate_scan.py` | **verified against the real crate** — switching and sensor verdicts confirmed there. Occupancy called an **empty** slot populated on 2026-08-27; fixed, see below |
| the crate | answers, switches, and reports sensible voltages and currents. Checked, and not the suspect for module-level faults. |

---

## 3. This crate

**Node id is 57 (0x39), not the factory default 63.** A bus scan on `can13` at
125 kbit/s found exactly one node — the cause of the server's initial
`SW Version ?.?`, which was polling a node that does not exist.
`config/config-elmbpsu.xml` now carries `id="57"`.

```
node 57 (0x39) state: PRE-OPERATIONAL (OPERATIONAL once the server drives it)
  hwVersion      = 0x30346C65   "el40"
  swVersion      = 0x3334414D   "MA43"
  swMinorVersion = 0x33303030   "0003"
  serialNumber   = 0x3234334B   "K342"
  guardTime      = 1000    lifeTime = 70

  dioOutputMaskC : 0xFF     <- both ports really are outputs
  dioOutputMaskA : 0xFF
  doInitHigh     : 0x01     <- outputs come up HIGH after a power cycle
  DO word        : 0xFFFF   <- all 16 branch bits read back as 1
```

A full 64-channel read through TPDO3 works and returns sensible values on
populated branches. **The original "every output pin measures 0 V" was a
measurement artifact** — the rails float, read nothing against chassis, and in
any case do not come out where they were being metered (REFERENCE.md §5). There
was never a fault to find there. **Polarity is the production one (1 = ON),
confirmed by measurement — do not use `--invert` on this crate.**

### An empty slot read as a module (2026-08-27, fixed)

`tests/crate_scan.py` reported slot 1 populated and then failing, on a crate with
**nothing in slot 1**:

```
Module 1: FAIL (fails to turn on: branches 2, 3 CAN V+AD V = 0.01 V;
                unsteady reading: branch 2 CAN I = 2.44 V at the ADC pin;
                current sensor does not work: branch 2 AD I = 0.27 V,
                branch 3 CAN I = 0.22 V, branch 3 AD I = 0.21 V)
```

Three of that slot's four current inputs floated at 0.21–0.27 V as an empty slot
should. The fourth, **branch 2 CAN I (ADC channel 12), wandered around 2.44 V** —
inside the ±0.1 V band around the transducer's 2.5 V zero, and the scan took one
sample landing in that band as proof of a live module. The rest of the report
followed from that: rails that could never come up were read as "fails to turn
on", and the genuinely floating channels as failed transducers.

The assumption that broke is in REFERENCE.md §4: an undriven current input is
**high impedance and free to drift anywhere**, the 2.5 V band included, so a
single reading there is not evidence of a transducer. Two rules now guard it, and
`tests/selftest.py` covers both plus the eight cases they must not break:

- a reading counts as a transducer only if the **repeat scans show it holding**
  that level — a powered source is a quiet DC level;
- and one sense line alone is not enough. Presence needs **a rail that came up**
  (only a module can raise one, the divider is on the module) **or two sense
  lines agreeing** (the four transducers share the module's housekeeping supply,
  so they come alive together).

Slot 1 now reads ABSENT, with a `!` note in the report naming the drifting
channel so the discounted reading is not silently swallowed. Slot 4 — one live
sense line but rails up — is unaffected and stays a real, faulty module.

### Current sensing is inside the module

Each branch current is measured by a **LEM HX 05-P/SP2** Hall-effect transducer
mounted **in the module**, in series with the branch output. Details and the
resulting plausibility limits are in REFERENCE.md §4. The consequence for
diagnosis: **an implausible current reading is a module fault**, since no
crate-side wiring is involved in producing that signal.

### `mon --source sdo` does not work on this crate

The on-request analog reads at 0x2404 return `Bad` through the server (`aisdo_0`,
`aisdo_1` both fail) while ordinary SDO reads on the same node are fine
(`do_C_read` = 255, `stateAsText` = OPERATIONAL). The ADC itself is configured and
scanning: `channelMax` = 64, `range` = 4, `mode` = 1, `aiTransmissionType` = 1,
and TPDO3 delivers. `mon` now defaults to `tpdo`; `sdo` is kept as the fallback
for a crate whose `aiTransmissionType` is not 1.

---

## 4. This machine (`pcaticstest08`, 2026-08-25)

**Shared.** Other users (`jsouter`, `kapoplaw`, `nkanello`) run WinCC OA projects
and CanOpenOpcUa here. Re-check with `./tests/can_diag.py` rather than trusting
this snapshot.

```
CAN adapters   4 x SYS TEC "Multiport CAN-to-USB" (0878:1101), USB 1-13.1..1-13.4
interfaces     can8..can15   (2 ports per box, ch0/ch1)
driver         systec_can (out-of-tree, already installed and loaded)
can-utils      installed (candump, cansend, cangen in /usr/bin)
can9           IN USE - the lab temperature monitor. Never touch.
can13          the ELMB PSU crate
```

Two OPC-UA servers answered a handshake:

| endpoint | owner | identity |
|----------|-------|----------|
| `opc.tcp://[::1]:4841` | jsouter | `OpcUaServer@pcaticstest08`, `urn:CERN:QuasarOpcUaServer` |
| `opc.tcp://[::1]:33815` | root | `CanOpen@pcaticstest08`, `urn:CERN:CanOpenOpcUa` |

**`/opt/labTempMonitor/bin/config.xml` is the lab temperature monitor's own
config: `<Bus port="can9" settings="125k">`, ELMB node 33** — pid 615812,
`./labTempMonitor` (a symlink to the CanOpenOpcUa binary), running as root since
10 July 2026 on `opc.tcp://pcaticstest08:33815`. That settles who owns `can9`.
**Never bring up, reconfigure or transmit on it.** Port 48012 — CanOpenOpcUa's
own default — was free, and is what we use.

One named network namespace exists (`netns-2ad82ea7-...`); CAN devices and ports
inside it are invisible from the host namespace, so `tests/can_diag.py` cannot
see them either.

- **Two CanOpenOpcUa builds are installed.** `/opt/labTempMonitor/bin/` is the
  tagged **v0.10.1** (28 Jun 2026) — use this one, it is what the lab runs.
  `/opt/CanOpenOpcUa/bin/` is a newer **v0.10.2-2-gf7bf9aa-dirty** pipeline build
  (20 Jul 2026). Both run and resolve their libraries.
- Python is **3.9.25** at `/usr/bin/python3`, and the tools are **kept
  3.6-compatible**. `socket.AF_CAN` and `CAN_RAW` are present; `python-can` is
  not installed and not needed.
- `.venv` therefore gets **asyncua 1.1.8**, the last release supporting 3.9.
  `~/.local/lib/python3.9/site-packages` also has 1.1.8, but the venv ignores user
  site-packages and installs its own.
- Other in-tree SocketCAN drivers if a different adapter turns up: `peak_usb`,
  `kvaser_usb`, `gs_usb`, `usb_8dev`, `slcan`, `peak_canfd`. An **AnaGate** needs
  no kernel driver but is **not** reachable by `lib/elmbpsu_can.py` — it would
  need `provider="an"` in the server config.

---
