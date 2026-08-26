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
- **The wider JCOP framework is not wanted here.** `fwInstallation-9.3.1/` and
  `jcop-framework-9.3.0.1/` are not fetched, not ignored, and not to be added.
- **Never take a CAN bus another user owns.** `can9` belongs to a production lab
  temperature monitor. Run `./tests/can_diag.py` before touching anything.
- **Never run two CANopen masters on one bus.** While the OPC-UA server is up,
  only `scan` and `dump` are safe from `lib/elmbpsu_can.py`.
- The user is hands-on with the hardware and wants **commands he can run**, not
  theory: minimal text, 1–5 sentences per step followed by the command.
- Be straight about what is verified against hardware versus simulated.

---

## 2. Layout

```
can_psu_test/                         the git repo and working directory
├── setup.sh                          restores the workspace: refs, venv, checks
├── README.md                         the full write-up
├── config/
│   ├── README.md                     which parameters to change
│   ├── config-elmbpsu.xml            CanOpenOpcUa crate config
│   └── ServerConfig-elmbpsu.xml      OPC-UA endpoint config (port 48012)
├── lib/
│   ├── elmbpsu_can.py                SocketCAN tool, zero dependencies
│   ├── elmbpsu_opcua.py              OPC-UA client (the WinCC OA replacement)
│   └── elmbpsu_server.py             start/stop the CanOpenOpcUa server itself
├── tests/
│   ├── can_diag.py                   read-only pre-flight (stdlib only)
│   ├── selftest.py                   offline verification, 27 checks
│   ├── smoke_test.py                 start server, scan bus, ping, stop server
│   └── crate_scan.py                 full sweep: occupancy, switching, sensors
├── docs/                             QUICKSTART, REFERENCE, HANDOFF
├── .venv/                            built by setup.sh, gitignored
└── CanOpenOpcUa/  fwElmb/  fwElmbPSU/    reference only, gitignored
```

```bash
git clone https://github.com/jimmat92/can_psu_test.git
cd can_psu_test
./setup.sh                 # add --ssh if you have a CERN GitLab SSH key
source .venv/bin/activate
```

`setup.sh` is idempotent. `--check` verifies without building; `--no-venv` skips
the venv; `--dest ..` places the reference repos as siblings; `--submodules`
inits CanModuleMain/LogIt if you intend to build the server.

The reference repos are **pinned to the exact commits that were read** —
REFERENCE.md cites specific files, and an upstream change could silently
invalidate a citation. `--latest` takes master instead, at that risk.

| repo | upstream (gitlab.cern.ch) | pinned commit | version |
|------|---------------------------|---------------|---------|
| `CanOpenOpcUa` | `atlas-dcs-opcua-servers/CanOpenOpcUa` | `a34bbabfeae8b501150b0d8f47728b8539914d09` | tag `v1.0.0` |
| `fwElmb` | `atlas-dcs-fwcomponents/fwElmb` | `094ecdd25ad5a3f19a1e37f4fa9415992e1bb426` | `9.4.6-15-g094ecdd` |
| `fwElmbPSU` | `atlas-dcs-fwcomponents/fwElmbPSU` | `101665b1983da85d56c65fb449fb22f298ca2468` | tag `9.2.3` |

All three are on CERN GitLab and may need credentials (a personal access token as
the HTTPS password, or an SSH key). `ElmbPsuIntroduction.pdf` needs no separate
fetch — an identical copy ships inside `fwElmbPSU/source/`.

---

## 3. Status

| | state |
|---|---|
| protocol, mappings, conversions | **verified** — against the JCOP sources, `tests/selftest.py` (27 checks), and against the crate |
| `lib/elmbpsu_can.py` | written, self-tested; `dump` verified on real hardware |
| `lib/elmbpsu_opcua.py` | **verified end-to-end against the real crate** on 2026-08-25 |
| `config/config-elmbpsu.xml` | **verified against the real crate** — server comes up, node table populates |
| `tests/can_diag.py` | verified both ways: detected a live server, correctly rejected sshd/cups/postfix |
| `tests/crate_scan.py` | analysis verified offline against replayed measurements; **never yet run against the crate** |
| the crate | answers, switches, and reports sensible voltages and currents. Checked, and not the suspect for module-level faults. |

---

## 4. This crate

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

### Current sensing is inside the module

Each branch current is measured by a **LEM HX 05-P/SP2** Hall-effect transducer
mounted **in the module**, in series with the branch output. Details and the
resulting plausibility limits are in REFERENCE.md §4. The consequence for
diagnosis: **an implausible current reading is a module fault**, since no
crate-side wiring is involved in producing that signal.

### Per-slot findings are not recorded here

Module positions have been changed since the first measurements, so any
slot-by-slot occupancy or fault list in this file would be stale. **The crate
itself has been checked and is not the suspect** — run `elmbpsu-cratescan` for
the current picture rather than trusting a written-down one.

### `mon --source sdo` does not work on this crate

The on-request analog reads at 0x2404 return `Bad` through the server (`aisdo_0`,
`aisdo_1` both fail) while ordinary SDO reads on the same node are fine
(`do_C_read` = 255, `stateAsText` = OPERATIONAL). The ADC itself is configured and
scanning: `channelMax` = 64, `range` = 4, `mode` = 1, `aiTransmissionType` = 1,
and TPDO3 delivers. `mon` now defaults to `tpdo`; `sdo` is kept as the fallback
for a crate whose `aiTransmissionType` is not 1.

---

## 5. This machine (`pcaticstest08`, 2026-08-25)

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
  (20 Jul 2026). Both run and resolve their libraries. An early draft assumed the
  latter, from `CanOpenOpcUa/Documentation/ExampleConfiguration/config-vcan0.xml`.
- Python is **3.9.25** at `/usr/bin/python3`. A 3.13.1 under `/usr/local/bin` has
  since disappeared, so **keep the tools 3.6-compatible**. `socket.AF_CAN` and
  `CAN_RAW` are present; `python-can` is not installed and not needed.
- `.venv` therefore gets **asyncua 1.1.8**, the last release supporting 3.9.
  `~/.local/lib/python3.9/site-packages` also has 1.1.8, but the venv ignores user
  site-packages and installs its own.
- Other in-tree SocketCAN drivers if a different adapter turns up: `peak_usb`,
  `kvaser_usb`, `gs_usb`, `usb_8dev`, `slcan`, `peak_canfd`. An **AnaGate** needs
  no kernel driver but is **not** reachable by `lib/elmbpsu_can.py` — it would
  need `provider="an"` in the server config.
- The `vcan` module is **absent** (needs kernel-modules-extra), so a virtual-CAN
  dry run is impossible. Use a free real port.

---

## 6. Corrections made along the way

Recorded because each was believed and acted on before being disproved.

**From the earlier ChatGPT session (`initial_discussion.txt`):**

1. Left the branch→bit mapping open. Resolved from the framework source: bit N =
   branch N.
2. Gave the server flag as `-c`. Only `--config_file`, or positional, exists.
3. Claimed the repo has no ELMB PSU config and the def files might be missing.
   They are in `CanOpenOpcUa/bin/` and
   `CanOpenOpcUa/Documentation/ExampleConfiguration/`; we inlined their content.

Its claims about terminators, the separate control bus, floating outputs, and
"powering the crate does not enable the branches" are all correct and confirmed.

**From this project's own earlier revisions:**

4. Claimed **no CAN hardware on the machine**. There are four SysTec adapters.
5. Described a nested `can_psu_test/can_psu_test/` layout with the reference
   repos as siblings. Wrong; see §2.
6. **Recommended `settings="DontConfigure"`,** inferred from `Design.xml` intent.
   It fails on both installed builds; reverted to `settings="125k"` + privileges
   (REFERENCE.md §6).
7. **Hypothesised an old-style crate (0 = ON)** from `DO word = 0xFFFF` plus a
   0 V meter reading. Acting on it would have switched every branch off — the
   populated branches read ~12 V with all bits at 1.
8. Claimed `elmbpsu_opcua.py` has no `--invert`. It does.
9. `mon` defaulted to `--source sdo`, which returns `Bad` here. Now `tpdo`.
10. `--method rpdo` wrote per-branch Booleans and hit the RPDO cache trap for
    real. Now writes the full 16-bit word (REFERENCE.md §6), verified with a
    no-op write of the already-latched value so no hardware state was changed.
11. `tests/crate_scan.py` read `stateAsText` the moment the endpoint opened and
    died with `BadWaitingForInitialData` (2026-08-26) — the endpoint being up is
    not the crate answering (REFERENCE.md §6). Both test scripts now use
    `PsuCrate.wait_ready()`.
12. Same script, other half of the same trap, reported 2026-08-26: the first run
    after a crate **power cycle** aborted on `node is PRE-OPERATIONAL` and only a
    re-run worked. The ELMB boots into PRE-OPERATIONAL and the server needs a
    node-guard cycle or two to drive it to OPERATIONAL. It now waits for
    OPERATIONAL rather than for the first readable state.
13. Cutting the scan's waits (2026-08-26) made it read the analog cache about a
    second after each switch, and **every module came back failing to switch** —
    the rails and the ELMB's ADC are both slower than that, so the scan was
    partly reading pre-switch values (REFERENCE.md §6). It now waits for the
    rails to hold still, and reports a rail it could not judge as unjudged
    instead of as a fault.

---

## 7. Open items

1. **Run `elmbpsu-cratescan` against the crate** — its analysis has only ever been
   exercised on replayed data, and it is now the primary diagnostic.
2. **Check the branch states** before and after: `elmbpsu-opcua status`, and
   `on all` if a previous session left branches off.
3. **Drop `syncIntervalMs` to ~1000** in the config before chasing anything that
   moves — at 10000 the sampling aliases a rail oscillation into nonsense.
   `elmbpsu-cratescan` already does this for its own server run.
4. **Establish what the DO bit drives** — TRACO Remote On/Off versus a series
   switch. Needs a module in hand; not answerable from software (REFERENCE.md §4)
   and does not affect operation.
5. The user has been offered the README as a shareable Artifact page and has not
   asked for it.
