# ELMB PSU project — handoff

Live state for an agent or colleague picking this up cold: what the task is,
what the crate and the machine are actually doing right now, what was corrected
along the way, and what is still open.

**Settled protocol, mapping and server knowledge has moved to
[REFERENCE.md](REFERENCE.md).** It is not repeated here. Operator procedure is
[QUICKSTART.md](QUICKSTART.md).

---

## 1. The task and its constraints

The user has a **CERN ELMB Power Supply Unit (PSU) crate** on the bench and
wants to test and operate it. He explicitly does **not** want to install
WinCC OA + fwElmbPSU + fwElmb. The agreed plan:

1. Run the CERN **CanOpenOpcUa** server against the crate's internal control ELMB.
2. Write a **custom client** that talks OPC-UA to that server, replacing WinCC OA.
3. Produce a correct **XML config** for the server.

Standing constraints:

- `fwElmb/`, `fwElmbPSU/` and `CanOpenOpcUa/` are **reference material only** —
  reverse-engineer from them, never install or run them. Cite the source file
  for any protocol claim.
- **The wider JCOP framework is not wanted here.** `fwInstallation-9.3.1/` and
  `jcop-framework-9.3.0.1/` are not fetched, not ignored, and not to be added.
  Nothing in this project has ever needed them.
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
├── docs/
│   ├── QUICKSTART.md                 operator procedure
│   ├── REFERENCE.md                  protocol, mappings, server behaviour
│   └── HANDOFF.md                    this file
├── .venv/                            built by setup.sh, gitignored
├── CanOpenOpcUa/   fwElmb/   fwElmbPSU/     reference only, gitignored
```

Origin: `https://github.com/jimmat92/can_psu_test.git`.

```bash
git clone https://github.com/jimmat92/can_psu_test.git
cd can_psu_test
./setup.sh                 # add --ssh if you have a CERN GitLab SSH key
source .venv/bin/activate
```

`setup.sh` clones the reference repos, builds `.venv` (asyncua, `lib/` on the
import path, the tools on `PATH`, `CAN_PSU_TEST`/`CAN_PSU_CONFIG` exported), and
verifies the workspace. `--check` verifies without building; `--no-venv` skips
the venv; `--dest ..` places the reference repos as siblings; `--submodules`
inits CanModuleMain/LogIt if you intend to build the server. It is idempotent.

The reference repos are **pinned to the exact commits that were read**, because
REFERENCE.md cites specific files and an upstream change could silently
invalidate a citation. `--latest` takes master instead, at that risk.

| repo | upstream (gitlab.cern.ch) | pinned commit | version |
|------|---------------------------|---------------|---------|
| `CanOpenOpcUa` | `atlas-dcs-opcua-servers/CanOpenOpcUa` | `a34bbabfeae8b501150b0d8f47728b8539914d09` | tag `v1.0.0` |
| `fwElmb` | `atlas-dcs-fwcomponents/fwElmb` | `094ecdd25ad5a3f19a1e37f4fa9415992e1bb426` | `9.4.6-15-g094ecdd` |
| `fwElmbPSU` | `atlas-dcs-fwcomponents/fwElmbPSU` | `101665b1983da85d56c65fb449fb22f298ca2468` | tag `9.2.3` |

All three are on CERN GitLab and may need credentials (a personal access token
as the HTTPS password, or an SSH key). `ElmbPsuIntroduction.pdf` needs no
separate fetch — an identical copy ships inside `fwElmbPSU/source/`.

---

## 3. Status

| | state |
|---|---|
| protocol, mappings, conversions | **verified** — against the JCOP sources, `tests/selftest.py` (27 checks), and now against the crate |
| `lib/elmbpsu_can.py` | written, self-tested; `dump` verified on real hardware |
| `lib/elmbpsu_opcua.py` | **verified end-to-end against the real crate** on 2026-08-25 |
| `config/config-elmbpsu.xml` | **verified against the real crate** — server comes up, node table populates |
| `tests/can_diag.py` | verified both ways: detected a live server, correctly rejected sshd/cups/postfix |
| the crate | answers, switches, and reports 11.8–12.8 V on ten of sixteen branches |

---

## 4. This crate (measured 2026-08-25)

**Node id is 57 (0x39), not the factory default 63.** A bus scan on `can13` at
125 kbit/s found exactly one node. This was the cause of the server's initial
`SW Version ?.?` — it was polling a node that does not exist.
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

Full 64-channel read through TPDO3:

```
branch     CAN V      CAN I      AD V       AD I
     0   11.841V    0.027A   11.910V    0.041A
     1   11.932V    0.053A    8.957V    0.017A     <- AD rail low, and drifting
     2   11.910V    0.006A   11.841V   -0.014A
     3   11.887V   -0.000A   11.856V    0.013A
     4   12.398V    0.044A   11.879V    0.003A
     5   12.619V    0.034A   12.551V    0.009A
     6   12.428V   -0.027A   11.902V  -18.050A     <- AD current channel floating
     7   11.849V  -18.606A   11.894V    6.551A     <- both current channels likewise
     8    0.015V  -19.820A    0.015V  -19.827A
     ...  (8..13 all ~0 V, current ~ -19.8 A)
    14   12.467V   -0.055A   12.505V    0.011A
    15   12.795V   -0.074A   12.490V   -0.044A
```

**The original "every output pin measures 0 V" was a measurement artifact.** The
rails float; measured against chassis they read nothing. There was never a fault
to find. **Polarity is the production one (1 = ON), confirmed by measurement —
do not use `--invert` on this crate.**

### Slot occupancy

| slots | branches | voltage inputs | current inputs | verdict |
|-------|----------|----------------|----------------|---------|
| 0,1,2,3,7 | 0–7, 14–15 | 11.8–12.8 V | at the 2.5 V zero (except below) | populated, powered |
| 4,5,6 | 8–13 | 76–152 µV (~0) | 264000–409000 µV (0.26–0.41 V) | **empty** |

Solid because the two input types fail differently — see REFERENCE.md §4. The
twelve empty-slot current channels span 0.26–0.41 V and no two agree, which is
the floating-input signature. Slot 7, physically the far end of the crate, reads
a healthy 12.47/12.51 V, so this is not "the scan stops after slot 3".
**Nobody has looked inside the crate — confirm physically.**

### Faults found

**Branch 1 (slot 0, position B) AD rail.** Sampled every 4 s through TPDO3:

```
br0 CAN   br0 AD   br1 CAN   br1 AD
 11.833   11.910   11.925   11.429
 11.841   11.910   11.925    6.470
 11.841   11.910   11.925    8.804
 11.833   11.917   11.932   11.704
 11.841   11.910   11.925    5.989
 11.833   11.910   11.925    8.263
 11.833   11.910   11.925   11.475
```

Everything else is steady to ±0.01 V; branch 1's AD rail swings between **5.99 V
and 11.70 V**. The user independently reported the "channel B" Va/d LED
flickering on the first module, which is this same branch — two independent
observations of one fault. The config uses `syncIntervalMs="10000"`, so these
samples are aliased; the real oscillation is faster than 0.1 Hz. Drop
`syncIntervalMs` to ~1000 to see its actual shape.

**Slot 3 (branches 6 and 7): three of four current sensors read as floating.**

| channel | role | raw µV | reading |
|---------|------|--------|---------|
| ch28 | br6 CAN I | 2496299 | −0.030 A, sensor correctly at its 2.5 V zero |
| ch29 | br6 AD I  | 243915  | 0.244 V — floating |
| ch30 | br7 CAN I | 174029  | 0.174 V — floating |
| ch31 | br7 AD I  | 3309910 | 3.310 V, i.e. +6.5 A on an unloaded rail |

ch29 and ch30 sit in the same 0.17–0.24 V band as the confirmed-empty slots, so
they look disconnected rather than wrong. All four of slot 3's *voltage*
channels are healthy (11.86–12.43 V) and one of its four current channels is
perfect. Some contacts good, some open → **partially seated module or damaged
sense harness**, not the output stage. Reseat slot 3 and re-read.

### Crate state left behind

The RPDO cache incident (REFERENCE.md §6) left **only branch 0 on**. Branches
1–7, 14 and 15 had been on since power-up and are now off. `on all` restores them.

### `mon --source sdo` does not work on this crate

The on-request analog reads at 0x2404 return `Bad` through the server (`aisdo_0`,
`aisdo_1` both fail) while ordinary SDO reads on the same node are fine
(`do_C_read` = 255, `stateAsText` = OPERATIONAL). The ADC itself is configured
and scanning: `channelMax` = 64, `range` = 4, `mode` = 1, `aiTransmissionType` = 1,
and TPDO3 delivers. The default for `mon` is now `tpdo`; `sdo` is kept as the
fallback for a crate whose `aiTransmissionType` is not 1.

---

## 5. This machine (`pcaticstest08`, 2026-08-25)

**Shared.** Other users (`jsouter`, `kapoplaw`, `nkanello`) run WinCC OA projects
and CanOpenOpcUa here. Re-check with `./tests/can_diag.py` rather than trusting this
snapshot.

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
config: `<Bus port="can9" settings="125k">`, ELMB node 33.** That is the
definitive answer to who owns `can9` — pid 615812, `./labTempMonitor` (a symlink
to the CanOpenOpcUa binary), running as root since 10 July 2026, serving
`opc.tcp://pcaticstest08:33815`. It is a production monitor on this bench.
**Never bring up, reconfigure or transmit on can9.** Port 48012 —
CanOpenOpcUa's own default — was free, and is what we use.

There is also one named network namespace (`netns-2ad82ea7-...`); CAN devices and
ports inside it are invisible from the host namespace, so `tests/can_diag.py` cannot
see them either.

Other environment facts:

- **Two CanOpenOpcUa builds are installed.** `/opt/labTempMonitor/bin/` is the
  tagged **v0.10.1** (28 Jun 2026) — use this one, it is what the lab runs.
  `/opt/CanOpenOpcUa/bin/` is a newer **v0.10.2-2-gf7bf9aa-dirty** pipeline build
  (20 Jul 2026). Both run and resolve their libraries. An early draft assumed
  `/opt/CanOpenOpcUa/bin/`, taken from
  `CanOpenOpcUa/Documentation/ExampleConfiguration/config-vcan0.xml`.
- Python is **3.9.25** at `/usr/bin/python3`. A 3.13.1 under `/usr/local/bin`
  existed earlier and is now gone, so **keep the tools 3.6-compatible** — do not
  assume anything newer. `socket.AF_CAN`/`CAN_RAW` are present. `python-can` is
  not installed and is not needed.
- `.venv` therefore gets **asyncua 1.1.8** (the last release supporting 3.9).
  `~/.local/lib/python3.9/site-packages` also has 1.1.8, but the venv ignores
  user site-packages and installs its own.
- Other in-tree SocketCAN drivers if a different adapter turns up: `peak_usb`,
  `kvaser_usb`, `gs_usb`, `usb_8dev`, `slcan`, `peak_canfd`. An **AnaGate** needs
  no kernel driver but is **not** reachable by `lib/elmbpsu_can.py` — it would
  need `provider="an"` in the server config.
- The `vcan` module is **absent** (needs kernel-modules-extra), so a virtual-CAN
  dry run is impossible. Use a free real port.

---

## 6. Corrections made along the way

Recorded because each one was believed and acted on before being disproved.

**From the earlier ChatGPT session (`initial_discussion.txt`):**

1. It left the branch→bit mapping open ("I would not yet assume that bit 0
   corresponds to branch 0"). **Resolved from the framework source:** bit N =
   branch N.
2. It gave the server flag as `-c` / `--config_file`. **Only `--config_file` (or
   positional) exists.**
3. It said the repo contains no explicitly named ELMB PSU config and implied the
   def files might be missing. They are present in `CanOpenOpcUa/bin/` and
   `CanOpenOpcUa/Documentation/ExampleConfiguration/`; we inlined their content.

Its claims about terminators, the separate control bus, floating outputs, and
"powering the crate does not enable the branches" are all correct and confirmed.

**From this project's own earlier revisions:**

4. An early HANDOFF claimed **no CAN hardware on the machine**. Obsolete — there
   are four SysTec adapters, can8–can15.
5. An early HANDOFF described a nested `can_psu_test/can_psu_test/` layout with
   the reference repos as siblings. Wrong; see §2.
6. **`settings="DontConfigure"` was recommended and does not work.** It was
   inferred from `Design.xml` intent and fails on both installed builds. Reverted
   to `settings="125k"` + privileges — REFERENCE.md §6.
7. **An old-style crate (0 = ON) was hypothesised and was wrong.** It came from
   `DO word = 0xFFFF` together with a 0 V meter reading. Acting on it would have
   switched every branch off. Ten of sixteen branches read 11.8–12.8 V with all
   bits at 1.
8. It was claimed `elmbpsu_opcua.py` has no `--invert`. It does.
9. `mon` defaulted to `--source sdo`, which returns `Bad` on this crate. Default
   changed to `tpdo`.
10. `--method rpdo` wrote per-branch Booleans and hit the RPDO cache trap for
    real. Now writes the full 16-bit word — REFERENCE.md §6. Verified with a
    no-op write of the already-latched value, so no hardware state was changed to
    test the fix.

---

## 7. Open items

1. **Restore the branches**: `elmbpsu-opcua on all`. Only branch 0 is on.
2. **Reseat slot 3** and re-read — three of its four current sensors read as
   floating (§4).
3. **Investigate branch 1's AD rail** (§4). Drop `syncIntervalMs` to ~1000 first
   to see the oscillation properly.
4. **Physically confirm slots 4, 5 and 6 are empty.** The software call is solid
   but nobody has opened the crate.
5. **Identify the Burndy pins** by the toggle method in REFERENCE.md §5, or get
   the pinout from EDMS **EDA-04145-V1-0** / the PH-ESS hardware page. It is not
   recoverable from anything in this workspace.
6. **Establish what the DO bit drives** — TRACO Remote On/Off versus a series
   switch. Needs a module in hand; not answerable from software (REFERENCE.md §4).
7. The user has been offered the README as a shareable Artifact page and has not
   asked for it.
