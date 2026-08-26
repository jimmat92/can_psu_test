# ELMB PSU project — handoff

Context dump for an agent picking this up cold. Nothing here is guesswork
unless explicitly flagged as such; every hardware/protocol claim carries the
source file it was derived from.

---

## 1. The task

The user (jimmat92@gmail.com) has a **CERN ELMB Power Supply Unit (PSU) crate**
on the bench. He powered it on: the AC display and output LEDs come up, but
**every output pin measures 0 V**. He wants to test and operate it.

He explicitly does **not** want to install WinCC OA + fwElmbPSU + fwElmb (the
official CERN control stack). The agreed plan is:

1. Run the CERN **CanOpenOpcUa** server against the crate's internal control ELMB.
2. Write a **custom client** that talks OPC-UA to that server, replacing WinCC OA.
3. Produce a correct **XML config** for the server.

The JCOP framework sources (`fwElmb`, `fwElmbPSU`) are in the workspace purely
as reference material to reverse-engineer the protocol from. They are not to be
installed or run.

---

## 2. Where everything is

```
/home/dmatakia/can_psu_test/                 <- the git repo AND working directory
├── setup.sh                                 restores this workspace on a new machine
├── can_diag.py                              read-only environment pre-flight
├── elmbpsu_can.py                           SocketCAN tool, zero dependencies
├── selftest.py                              offline verification of the above
├── config-elmbpsu.xml                       CanOpenOpcUa server config
├── elmbpsu_opcua.py                         OPC-UA client (the WinCC OA replacement)
├── README.md                                full write-up + troubleshooting
├── QUICKSTART.md                            short operator procedure
├── HANDOFF.md                               this file
├── .gitignore                               written by setup.sh
├── CanOpenOpcUa/                            reference: the OPC-UA server source
├── fwElmb/                                  reference: JCOP ELMB component
└── fwElmbPSU/                               reference: JCOP ELMB PSU component
```

**The layout changed.** An earlier revision of this file described a nested
`can_psu_test/can_psu_test/` with the reference material as siblings. What is
actually on disk now is a plain `git clone` plus `./setup.sh` at its default
`--dest .`, so the three reference repos sit *inside* the repo (and are
gitignored). `ElmbPsuIntroduction.pdf`, `initial_discussion.txt`,
`fwInstallation-9.3.1/` and `jcop-framework-9.3.0.1/` are **not** present here;
an identical copy of the PDF ships inside `fwElmbPSU/source/`.

Git: `origin = https://github.com/jimmat92/can_psu_test.git`, HEAD
`81aebb8 Added env setup script`.

### Restoring this workspace elsewhere

```bash
git clone https://github.com/jimmat92/can_psu_test.git
cd can_psu_test
./setup.sh                 # add --ssh if you have a CERN GitLab SSH key
```

`setup.sh` clones the three reference repos, writes `.gitignore`, and verifies
that every source file cited in section 4 is present plus that the toolchain
works (`--check` verifies without cloning; `--pip` installs asyncua;
`--submodules` inits CanModuleMain/LogIt if you intend to build the server;
`--dest ..` places the repos as siblings instead of inside this repo, which is
the layout on the user's machine). It is idempotent and detects repos already
present in either location.

The reference repos are **pinned to the exact commits that were read**, because
section 4 cites specific files and an upstream change could silently invalidate
a citation. `--latest` takes master instead, at that risk.

| repo | upstream (gitlab.cern.ch) | pinned commit | version |
|------|---------------------------|---------------|---------|
| `CanOpenOpcUa` | `atlas-dcs-opcua-servers/CanOpenOpcUa` | `a34bbabfeae8b501150b0d8f47728b8539914d09` | tag `v1.0.0` |
| `fwElmb` | `atlas-dcs-fwcomponents/fwElmb` | `094ecdd25ad5a3f19a1e37f4fa9415992e1bb426` | `9.4.6-15-g094ecdd` |
| `fwElmbPSU` | `atlas-dcs-fwcomponents/fwElmbPSU` | `101665b1983da85d56c65fb449fb22f298ca2468` | tag `9.2.3` |

All three are on **CERN GitLab and may need credentials** (a personal access
token as the HTTPS password, or an SSH key). `ElmbPsuIntroduction.pdf` needs no
separate fetch — an identical copy ships inside `fwElmbPSU/source/`, alongside
the editable `.doc`. `fwInstallation-9.3.1/` and `jcop-framework-9.3.0.1/` are
deliberately **not** fetched; nothing here needs them (see section 8).

---

## 3. The answer to "why 0 V"

**This is the expected state, not a fault.** Branch outputs are not enabled by
mains power. The crate contains one control ELMB whose digital output ports A
and C drive the on/off switch for the 16 branches
(`ElmbPsuIntroduction.pdf` §2). At power-up those outputs assume whatever is
stored in ELMB object **0x2300 `doInitHigh`**, which on an unconfigured crate
leaves every branch off. The front-panel LEDs are module/AC status, not 12 V
presence on the Burndy.

Two facts that matter when the user goes back to the meter:

- Each branch's Burndy carries **two independent 12 V rails** — a *CAN* rail
  (~25 W) and an *AD* (analog/digital) rail (~35 W). They are monitored
  separately but switched together by **one** control bit. This is what the user
  meant by "2 outputs per CAN bus".
- Both rails **float**. Measuring a positive pin against chassis reads nothing
  meaningful. Measure each positive against **its own return**.

The 120 Ω resistors in the ELMB/CAN network are CAN-line terminators only. They
have nothing to do with enabling the output. No load is required.

---

## 4. The reverse-engineered protocol (the critical knowledge)

### 4.1 Bus and node

| item | value | source |
|------|-------|--------|
| control-bus bitrate | **125 kbit/s** default | `ElmbPsuIntroduction.pdf` §2 |
| control ELMB node id | **63 (0x3F)** default | `ElmbPsuIntroduction.pdf` §2 |
| control bus terminators | **none built in** — user must fit 120 Ω at both ends | `ElmbPsuIntroduction.pdf`, "Terminators" |
| branch bus terminators | built into the modules, jumpers ST1/ST2 | same |

Node 63 is a *default*, not a guarantee: standard ELMB firmware sets the node id
from 6 bits (range 0–63, and 63 is all-ones = the unprogrammed state), and a
crate that once shared a bus with others will have been changed. Corroborating
evidence: `fwElmb/fwElmb_Readme.txt:110` lists "Can now choose node IDs greater
than 63 (for custom ELMB firmware)" as a feature. Nothing in the framework code
hardcodes 63 — always confirm with a bus scan.

The control bus is **electrically and logically separate** from the powered
branches; it needs its own CAN interface port on the PC. DE-9 pinout per
CiA-303: pin 2 = CAN-L, pin 3 = CAN GND, pin 7 = CAN-H, pin 9 = optional V+.

**Back-plane isolation switch.** The crate has a switch on its back-plane that
connects or isolates the internal ELMB's supply from the control CAN bus
(`ElmbPsuIntroduction.pdf` §2). With more than one crate on a bus it *must* be
set to **Isolated**. With a single crate on a bench setup either position can
work, but it decides whether the crate feeds power onto the control bus - check
it before assuming the CAN interface must supply V+ on DE-9 pin 9.

**External references cited by the PDF, not yet followed up:**
- PH-ESS hardware page: <http://ess.web.cern.ch/ESS/canpsuProject/index.htm>
  - the most likely public source for the **Burndy pinout**, still an open item
  (see section 8).
- Branch connection scheme guidance, EDMS:
  <https://edms.cern.ch/file/685351//CANbus_Guideline.pdf>
- fwElmbPSU / fwElmb downloads:
  <http://atlas.web.cern.ch/Atlas/GROUPS/DAQTRIG/DCS/ELMB/DIST/ELMBdoc.html>

### 4.2 Branch numbering

From `ElmbPsuIntroduction.pdf` Figure 4 (verified by rendering page 7 of the PDF
and reading the screenshot, not from prose alone):

```
        slot 0   slot 1   slot 2   slot 3   slot 4   slot 5   slot 6   slot 7
pos A     0        2        4        6        8       10       12       14
pos B     1        3        5        7        9       11       13       15

branch = 2*slot + (0 for position A/top, 1 for position B/bottom)
```

Up to 8 modules, 2 branches each, 16 branches total.

### 4.3 Branch → digital output bit  ← THE KEY MAPPING

`fwElmbPSU_createPowerControl()` in
`fwElmbPSU/scripts/libs/fwElmbPSU/fwElmbPSU.ctl`:

```
if (argiBranchNumber > 7) { sPort = "A"; iBit = argiBranchNumber - 8; }
else                      { sPort = "C"; iBit = argiBranchNumber;     }
```

`fwElmbUser_setDoBits()` and `fwElmbUser_getDoBytes()` in
`fwElmb/scripts/libs/fwElmb/fwElmbUser.ctl` combine the two ports into one
16-bit word:

```
port A -> mask = 1 << (bit + 8)
port C -> mask = 1 << bit
getDoBytes: uResult = (uPortA << 8) | uPortC
```

Therefore:

> **Bit N of the 16-bit DO word is branch N**, for all 16 branches. No
> reordering, no surprises.

The earlier ChatGPT notes in `initial_discussion.txt` flagged this as unresolved
("I would not yet assume that bit 0 corresponds to branch 0"). It is now
resolved from the framework source.

### 4.4 CANopen objects on the control ELMB

From `fwElmb/config/fwElmb/OPCUA_nodeType_ELMB.xml` and the
`CanOpenOpcUa/bin/CANopen_def_STDELMB_*.xmle` fragments:

| object | meaning |
|--------|---------|
| RPDO1, COB-ID `0x200 + node` | `do_write`, UInt16 at byte offset 0. **byte 0 = port C, byte 1 = port A.** This is the path fwElmbPSU actually uses. Requires the node to be OPERATIONAL. |
| SDO `0x6200:01` / `:02` | `do_C_read` / `do_A_read` — read-back of the output latches |
| SDO `0x6220:01..08` | `do_C_0..7` bitwise write (branches 0–7) |
| SDO `0x6220:09..16` | `do_A_0..7` bitwise write (branches 8–15) — so sub-index = branch + 1 uniformly |
| SDO `0x6208:01` / `:02` | `dioOutputMaskC` / `dioOutputMaskA`, 1 = pin is an output. Must be `0xFF`/`0xFF` or nothing can switch. |
| SDO `0x2300:00` | `doInitHigh` — output state at ELMB power-up |
| SDO `0x2404:01..0x40` | `aisdo_0..63`, on-request analog reads, Int32 |
| TPDO3, COB-ID `0x380 + node` | multiplexed 64-channel analog scan, SYNC-driven, needs `aiTransmit.aiTransmissionType == 1` (SDO `0x1802:02`) |
| SDO `0x2100:01..04` | `channelMax`, `rate`, `range`, `mode` (ADC config) |
| SDO `0x1009`, `0x100A:00/:01`, `0x3100` | hwVersion, swVersion/swMinorVersion, serialNumber |
| SDO `0x1010:01` | `save` — persist parameters to EEPROM (write the ASCII `"save"` as UInt32) |

### 4.5 Polarity

`fwElmbPSU/scripts/libs/fwElmbPSU/fwElmbPSUConstants.ctl`:

```
// This is for new version of PSU
const bool EPSU_POWER_ON_VALUE = 1;
const bool EPSU_POWER_OFF_VALUE = 0;
// This is for old version of PSU
//const bool EPSU_POWER_ON_VALUE = 0;
//const bool EPSU_POWER_OFF_VALUE = 1;
```

Production ("new") crates: `1 = ON`. Pre-2.0.0 ("old") crates: inverted.
`fwElmbPSU_Readme.txt` states fwElmbPSU ≥ 2.0.0 supports production crates only.
Both of our tools default to the production polarity and accept `--invert`.

### 4.6 Monitoring channels and unit conversion

`fwElmbPSU_createMonitorChannel()` computes, for branch *b*, the base channel
`ch = 4*b - 2*(b mod 2)`:

| quantity | ELMB analog input |
|----------|-------------------|
| CAN voltage | `ch` |
| AD voltage | `ch + 1` |
| CAN current | `ch + 4` |
| AD current | `ch + 5` |

That tiles all 64 ELMB inputs exactly once, 8 per module (verified in
`selftest.py`). Raw values are **signed microvolts at the ADC input**. The
sensor formulas installed by `fwElmbPSU/scripts/fwElmbPSU/fwElmbPSU.postInstall`
are `"%c1*%x1/1000000.0"` with x1 = 100.0, and
`"((%c1/1000000.0)-%x1)*%x2/%x3"` with x1 = 2.5, x2 = 5.0, x3 = 0.625:

```
voltage [V] = raw/1e6 * 100.0                 (100:1 divider on the module)
current [A] = (raw/1e6 - 2.5) * 5.0 / 0.625   (2.5 V-centred sensor, 8 A/V)
```

Nominal per branch, from `fwElmbPSUConstants.ctl`: 12.0 V on both rails, 20 mA
CAN / 25 mA AD unloaded.

### 4.7 One unresolved ambiguity (handled, not eliminated)

`fwElmbUser_createOPCFile()` in `fwElmbUser.ctl` — a **legacy** generator for
the old Slava OPC-DA server — labels RPDO1 byte 0 as **PORTF**, not PORTC. The
current, maintained model (`OPCUA_nodeType_ELMB.xml`, the `.xmle` fragments, and
the read-back logic in `setDoBits`, which compares written vs read and logs
"INCOHERENT" on mismatch) all say **byte 0 = port C**. We went with C/A.

This is made harmless by design: every switch operation in both tools reads
`0x6200` back and reports a mismatch, and `--method sdo` bypasses the RPDO
entirely by writing each bit through its own object-dictionary entry. If exactly
the wrong half of the branches respond, this is the cause.

---

## 5. What was built

All four deliverables were written from scratch and **verified against
simulators**, since no CAN hardware is attached (see §6).

### `elmbpsu_can.py` — direct SocketCAN tool

Speaks CANopen straight over `AF_CAN`/`CAN_RAW` using **only the Python
standard library** — no `python-can`, no build step. This is the fast path for
bring-up and diagnosis, independent of the whole OPC-UA stack.

Implements expedited SDO upload/download, NMT, node guarding, and RPDO1
transmission. Subcommands: `scan` (probes node ids 1–127), `info`, `status`,
`nmt`, `on`, `off`, `mon`, `dump` (passive candump), `outmask`. Global flags:
`--iface`, `--node`, `--timeout`, `--invert`, `-v` (frame trace).

Every `on`/`off` reads `0x6200` back and prints `read-back OK` or a
`READ-BACK MISMATCH` with the specific likely causes.

### `can_diag.py` — environment pre-flight (added 2026-08-25)

Written because the machine turned out to be shared and to already have CAN
hardware and a CanOpenOpcUa server on it. Read-only: it configures no link and
transmits no CAN frame. Stdlib only, except for one optional `asyncua` call.
Three sections:

1. **OPC-UA servers.** Listening TCP sockets are read from `/proc/net/tcp{,6}`
   rather than via `ss -p`, because `ss`/`netstat` hide the process for another
   user's socket while the uid column in `/proc` never does. Each plausible
   port is then greeted with a real OPC-UA UACP `HELLO` (OPC 10000-6 §7.1.2):
   `ACK` or `ERR` proves an OPC-UA server, anything else disproves it. Validated
   both ways — a live `asyncua` server on 48012 was detected, and
   sshd/cups/postfix/WinCC OA ports were correctly rejected. Confirmed servers
   are then asked `GetEndpoints` for their ApplicationName/Uri, which is what
   identified the `urn:CERN:CanOpenOpcUa` instance noted in section 6.
2. **CAN interfaces.** `/sys/class/net/*/type == 280` (ARPHRD_CAN) enumerates
   them; `ip -details -json link show` supplies CAN state and bitrate (sysfs
   does not carry those); `/sys/class/net/*/statistics/*` sampled twice gives a
   live frame rate; the sysfs `device` symlink is walked up to the USB parent so
   ports are grouped by the physical box they share.
3. **Used vs. unused.** The authoritative signal is `/proc/net/can/rcvlist_*`,
   the kernel's per-device receive filter list. An entry there means some
   process holds an open socket bound to that device, and it is readable by any
   user without root. That is cross-checked against the live frame rate and
   attributed to a PID via `lsof` (which labels AF_CAN sockets as
   `protocol: CAN_RAW`), with a `/proc/*/fd` scan as fallback.

**Known limit, stated in the output:** rcvlist names the *device* but not the
process; lsof names the *process* but not the device. Nothing in the kernel's
user-visible interface joins them. The script joins them only where a command
line names an interface (`candump can11`) or the match is unambiguous, and says
so plainly otherwise. Without root you cannot read other users' `/proc/*/fd`, so
the PID list is yours alone — the socket *counts*, being kernel-side, still
cover everybody.

`--json` emits the same data machine-readably; `--no-probe` suppresses all
outbound connections; `--sample 0` skips the traffic window; `--no-identify`
skips the asyncua call.

### `selftest.py` — offline verification

Simulates an ELMB responder and exercises the tool without hardware: SDO
encode/decode, both bit-mapping paths (RPDO and bitwise SDO), the analog channel
map, the unit conversions, branch labels, spec parsing. **27 checks, all
passing.** Run it before blaming hardware.

### `config-elmbpsu.xml` — CanOpenOpcUa configuration

Deliberately **self-contained**: it does not use the
`CANopen_def_STDELMB_*.xmle` entity includes, so there is no DTD path to get
wrong. Equivalent content is inlined plus PSU-specific extras. XML
well-formedness verified; attribute names and enum values hand-checked against
`CanOpenOpcUa/Design/Design.xml`.

Beyond the stock ELMB model it exposes: 16 per-branch Booleans on RPDO1 (so a
client flips one branch without its own read-modify-write), the raw `do_write`
word, the `0x6220` bitwise objects, the `0x6208` direction masks, `doInitHigh`,
and the `0x2404` on-request analog reads (so monitoring works even if the ELMB
is not configured for SYNC/TPDO3).

Things a follow-up agent must know about it:
- `RpdoCachedVariable` with `dataType="Boolean"` + `booleanToBit` does a
  read-modify-write of the 8-byte RPDO cache and then transmits
  (`Device/src/DRpdoCachedVariable.cpp` `writeValue` → `propagateCache`).
- The RPDO always transmits **8 data bytes** (`DRpdo.cpp` assigns
  `m_cache.assign(8, 0)`), even though the ELMB DO mapping is 2 bytes. This is
  stock CanOpenOpcUa behaviour used in production at CERN.
- `Boolean` packs to exactly 1 byte for SDO writes
  (`Device/src/ValueMapper.cpp`).
- `SdoVariable` `dataType` enum has **no Int16**: Boolean, Byte, UInt16, UInt32,
  Int32, ByteString.
- Multiplexed TPDO channels are auto-named **`ch0`..`ch63`** with `id = chno`
  (`Server/src/ConfigurationProcessing.cxx`); the specimen element must literally
  be named `specimen`.
- quasar builds string node ids by dot-joining the hierarchy
  (`AddressSpace/src/ASNodeManager.cpp` `makeChildNodeId`). Namespace URI is
  `"OPCUASERVER"` (`NodeManagerBase("OPCUASERVER", ...)`), normally ns index 2.

### `elmbpsu_opcua.py` — OPC-UA client (the WinCC OA replacement)

Uses `asyncua` (sync API). Resolves the `OPCUASERVER` namespace index at
runtime rather than assuming 2. Writes **explicitly-typed Variants** — quasar
declares these nodes as `BaseDataType`, so an untyped write is rejected.
Subcommands: `status`, `on`, `off`, `word`, `mon` (`--source sdo|tpdo`),
`browse`. Verifies every switch by reading `0x6200` back.

Node-id paths it constructs:
```
<bus>.<node>.RPDO1.do_write            UInt16
<bus>.<node>.RPDO1.branch00 .. 15      Boolean
<bus>.<node>.do_bitwise.do_C_0 ...     Boolean
<bus>.<node>.do_read.do_C_read / do_A_read
<bus>.<node>.dioOutputMask.dioOutputMaskC / ...A
<bus>.<node>.doInitHigh
<bus>.<node>.stateAsText
<bus>.<node>.aisdo.aisdo_0 .. 63
<bus>.<node>.TPDO3.ch0.value / .adcFlag
```

**Verified end-to-end** against a purpose-built mock OPC-UA server reproducing
that exact address space: `status`, `on` (rpdo), `on --method sdo`, `off`,
`word`, `mon --source sdo`, `mon --source tpdo`, `browse` all worked and all
read-backs matched.

### `README.md` and `QUICKSTART.md`

README is the full write-up with source citations and a 5-step troubleshooting
ladder (§6) for "branch still shows 0 V after a verified switch-on". QUICKSTART
is the short operator procedure: pick a free port with `can_diag.py` → bring
it up → start the server → CAN
debugging → status → switch → read.

---

## 6. Environment state — READ THIS BEFORE PROMISING ANYTHING

**CAN hardware is now present. Nothing has yet been tested against the PSU
crate itself** — sections 4 and 5 are still verified against simulators only —
but the bus side of this environment has changed completely since this document
was first written. Re-check it with `./can_diag.py` rather than trusting the
snapshot below.

As of 2026-08-25 on `pcaticstest08`:

```
CAN adapters   4 x SYS TEC "Multiport CAN-to-USB" (0878:1101), USB 1-13.1..1-13.4
interfaces     can8..can15   (2 ports per box, ch0/ch1)
driver         systec_can (out-of-tree, already installed and loaded)
can-utils      installed (candump, cansend, cangen in /usr/bin)
can9           UP, 125 kbit/s, ~12 frames/s, 30M frames, 1 socket bound -- IN USE
can11          UP, 125 kbit/s, no socket bound, ~72k frames historically
can8/10/12-15  DOWN, unbound -- free
```

**This is a shared machine.** Other users (`jsouter`, `kapoplaw`, `nkanello`)
run WinCC OA projects and CanOpenOpcUa on it. Two OPC-UA servers answered a
handshake during this work:

| endpoint | owner | identity |
|----------|-------|----------|
| `opc.tcp://[::1]:4841` | jsouter | `OpcUaServer@pcaticstest08`, `urn:CERN:QuasarOpcUaServer` |
| `opc.tcp://[::1]:33815` | root | `CanOpen@pcaticstest08`, `urn:CERN:CanOpenOpcUa` |

The second is a **CanOpenOpcUa server already running here**, almost certainly
the `podman run --privileged -v /home/jsouter/can-dev-4/CanOpenOpcUa:...`
container (pid 3117043), and the most likely owner of `can9`. Port 48012 —
CanOpenOpcUa's own default — was free. There is also one named network
namespace (`netns-2ad82ea7-...`); CAN devices and ports inside it are invisible
from the host namespace, so `can_diag.py` cannot see them either.

Do not take a bus without checking first. That is what `can_diag.py` is for,
and it is read-only.

Other in-tree SocketCAN drivers, if a different adapter turns up: `peak_usb`,
`kvaser_usb`, `gs_usb`, `usb_8dev`, `slcan`, `peak_canfd`. An **AnaGate** needs
no kernel driver but is *not* reachable by `elmbpsu_can.py` — it would need
`provider="an"` in the server config instead. The `vcan` module is still absent
(needs kernel-modules-extra), so a virtual-CAN dry run remains impossible; use a
free real port instead.

**`CanOpenOpcUa` in this workspace is not buildable as-is.** The `CanModuleMain`
and `LogIt` submodule directories are **empty**:

```bash
cd /home/dmatakia/can_psu_test/CanOpenOpcUa
git submodule update --init --recursive
```

It further needs an OPC-UA backend (open62541-compat is the free one), Boost,
XSD/xerces and the quasar toolchain; `quasar.py` drives the build. If ATLAS DCS
RPMs are available, installing the prebuilt package is far less work.

Other environment facts:
- Python **3.13.1** at `/usr/local/bin/python3` and **3.9.25** at
  `/usr/bin/python3`; `socket.AF_CAN`/`socket.CAN_RAW` present in both. Note
  that `#!/usr/bin/env python3` resolves to **3.9** in a default login shell, so
  keep the tools 3.6-compatible.
- **`asyncua` 2.0.1 was pip-installed** into
  `/usr/local/lib/python3.13/site-packages/`; `asyncua` 1.1.8 is also present
  for 3.9. `python-can` is not installed and is not needed.
- **Two CanOpenOpcUa builds are installed.** `/opt/labTempMonitor/bin/` is the
  tagged **v0.10.1** (28 Jun 2026) and the one the user's colleague pointed at;
  `/opt/CanOpenOpcUa/bin/` is a newer **v0.10.2-2-gf7bf9aa-dirty** pipeline build
  (20 Jul 2026). Both run and resolve all their libraries. Use labTempMonitor's.
- **`--opcua_backend_config` defaults to `<binary dir>/ServerConfig.xml`** —
  `getApplicationPath()`, not the CWD (`BaseQuasarServer.cpp:308`, and `--help`
  prints the resolved path). For the labTempMonitor build that file declares
  endpoint port **33815**, which the running lab temperature monitor already
  holds, so the flag must be passed explicitly. `ServerConfig-elmbpsu.xml` in
  this repo is that file with the port changed to 48012 (the only diff).
- **`/opt/labTempMonitor/bin/config.xml` is the lab temperature monitor's own
  config: `<Bus port="can9" settings="125k">`, ELMB node id 33.** That is the
  definitive answer to who owns `can9` — pid 615812, `./labTempMonitor` (a
  symlink to the CanOpenOpcUa binary), running as root since 10 July 2026, and
  serving `opc.tcp://pcaticstest08:33815`. It is a production monitor on this
  bench. Never bring up, reconfigure or transmit on can9.
- **The server cannot run unprivileged against real SocketCAN hardware.**
  Verified 2026-08-25 on both installed builds against `can13`. CanModule's
  socketcan vendor always enters `CanVendorSocketCan.cpp:49 "Configuring
  SocketCAN device"` and shells out to `ip` to stop the link, set the bitrate and
  restart it. Outcomes:

  | build | settings | result |
  |-------|----------|--------|
  | v0.10.1 | `125k` / `DontConfigure` | `RTNETLINK answers: Operation not permitted` on both stop and bitrate -> `Failed to open CAN device: SOCKET_ERROR` |
  | v0.10.2 | `DontConfigure` | no stop attempt; bitrate 0 -> `RTNETLINK answers: Invalid argument`, same failure, and **the port is left DOWN** |

  Once the device fails to open, every frame gives
  `Failed to send CAN frame: error code UNKNOWN_SEND_ERROR` and the startup SDO
  read times out (`SW Version ?.?` in the node table). The OPC-UA endpoint still
  opens normally, which makes the failure easy to miss.

  `--force_dont_reconfigure` does **not** help: it logs `note: forcing
  DontReconfigure mode as per command line args` (`DBus.cpp:107`) and then does
  nothing, because `settings = "Unspecified";` on the next line is **commented
  out** (`Device/src/DBus.cpp:109`). `DontConfigure` maps to bitrate 0
  (`DBus.cpp:390`), which CanModule does not treat as "skip". `--map_to_vcan` is
  the only thing that sets `vcan=true`, and it rewrites the port name to
  `vcan<N>`, so it is useless for real hardware.

  **Therefore: use `settings="125k"` and run with privileges** — `sudo`, or
  `setcap cap_net_admin+ep` on a private copy of the binary (`/home` is not
  `nosuid`, so file capabilities work). This is exactly what the lab's own
  labTempMonitor does: `settings="125k"`, running as root.
- The raw tool needs none of this. `elmbpsu_can.py` binds an `AF_CAN` socket and
  never touches link configuration, so it works as an ordinary user — verified
  with a passive `dump` on can13. It does require the link to already be up.
- Server lifecycle, verified 2026-08-25 with a bus-less config on port 48012:
  starts fine under `nohup ... &`, binds the endpoint, and **exits cleanly on
  plain SIGTERM** (`kill <pid>`) as well as SIGINT. `systemd-run --user` also
  works, but this account cannot read the user journal (`Linger=no` too), so
  redirect stdout/stderr to a file regardless of method.
- Security is `None` with `EnableAnonymous=true`, and the PKI paths in
  ServerConfig.xml (`/localdisk/tmp/PKI/CA/certs/`) exist and are world-writable,
  so no certificate work is needed.
- The old assumed server binary path was `/opt/CanOpenOpcUa/bin/` (from
  `CanOpenOpcUa/Documentation/ExampleConfiguration/config-vcan0.xml`).
- Endpoint from `CanOpenOpcUa/bin/ServerConfig.xml`:
  `opc.tcp://[NodeName]:48012`, anonymous, security policy `None`.

### CanOpenOpcUa command line (checked in `Server/src/BaseQuasarServer.cpp`)

`--config_file` (also accepted positionally), `--opcua_backend_config`,
`--create_certificate`, `--help/-h`, `--version/-v`, `--version_extra`.
Project-specific extras from `Server/src/QuasarServer.cpp`: `--l<Component>
<LEVEL>` where Component ∈ {CanModule, Emergency, NodeMgmt, Rpdo, Sdo,
SdoValidator, Spooky, Spy, NmTpdo, MTpdo} and LEVEL ∈ {ERR,WRN,INF,DBG,TRC};
plus `--Wall`, `--Wnone`, `--W<warning>`, `--Wno_<warning>`,
`--force_dont_reconfigure`, `--map_to_vcan`, `--print_cobids_tables`.

There is **no `-c` short option** — the earlier ChatGPT notes and an early draft
of our README both had this wrong; it is now corrected everywhere.

---

## 7. Corrections made to the earlier ChatGPT session

`initial_discussion.txt` is mostly sound but three things were wrong or
unresolved:

1. It left the branch→bit mapping open. **Resolved:** bit N = branch N (§4.3).
2. It gave the server flag as `-c` / `--config_file`. **Only `--config_file`
   (or positional) exists.**
3. It said "The repository does not contain an explicitly named
   ELMB_PSU_config.xml" and implied the def files might be missing. They are
   present in `CanOpenOpcUa/bin/` and `CanOpenOpcUa/Documentation/
   ExampleConfiguration/`; we inlined their content rather than referencing them.

Its claims about terminators, the separate control bus, floating outputs, and
"powering the crate does not enable the branches" are all correct and confirmed.

---

## 8b. First contact with the real crate (2026-08-25)

The crate answered for the first time on this date. Everything below is measured,
not inferred.

**Node id is 57 (0x39), not 63.** A bus scan on `can13` at 125 kbit/s found
exactly one node. Section 4.1 flagged 63 as a default rather than a guarantee;
that caution was justified. `config-elmbpsu.xml` now carries `id="57"`.

```
node 57 (0x39) state: PRE-OPERATIONAL
  hwVersion      = 0x30346C65   "el40"
  swVersion      = 0x3334414D   "MA43"
  swMinorVersion = 0x33303030   "0003"
  serialNumber   = 0x3234334B   "K342"
  guardTime      = 1000    lifeTime = 70
```

**The digital outputs are configured, commanded ON, and actually delivering.**

```
dioOutputMaskC  : 0xFF        <- both ports really are outputs
dioOutputMaskA  : 0xFF
doInitHigh      : 0x01        <- outputs come up HIGH after a power cycle
DO word         : 0xFFFF      <- all 16 branch bits read back as 1
NMT state       : OPERATIONAL <- once the server drove it there
```

`./elmbpsu_opcua.py mon --source tpdo`, read through the server from the crate's
own ADC:

```
branch     CAN V      CAN I      AD V       AD I
     0   11.841V    0.027A   11.910V    0.041A
     1   11.932V    0.053A    8.957V    0.017A     <- AD rail low, and drifting
     2   11.910V    0.006A   11.841V   -0.014A
     3   11.887V   -0.000A   11.856V    0.013A
     4   12.398V    0.044A   11.879V    0.003A
     5   12.619V    0.034A   12.551V    0.009A
     6   12.428V   -0.027A   11.902V  -18.050A     <- AD current channel not at 2.5V zero
     7   11.849V  -18.606A   11.894V    6.551A     <- both current channels likewise
     8    0.015V  -19.820A    0.015V  -19.827A
     ...  (8..13 all ~0 V, current ~ -19.8 A)
    14   12.467V   -0.055A   12.505V    0.011A
    15   12.795V   -0.074A   12.490V   -0.044A
```

**Polarity is the production one (1 = ON), confirmed by measurement.** An earlier
revision of this file hypothesised an old-style crate (0 = ON) on the strength of
`DO word = 0xFFFF` together with a 0 V meter reading. That hypothesis was
**wrong** and acting on it (adding `--invert`) would have switched every branch
off. Ten of sixteen branches read 11.8-12.8 V on both rails with all bits at 1.
Do not add `--invert` on this crate.

**The original "every output pin measures 0 V" was a measurement artifact.** The
rails float; measured against chassis they read nothing. Section 3 said so and it
turned out to be the whole story. There was never a fault to find.

**Reading branches 8-13.** Both rails ~0.01 V and both currents ~ -19.8 A. That
is the signature of an absent module, not a failed one: an unpopulated ADC input
reads ~0 uV, and 0 V through the current formula gives (0 - 2.5) x 8 = -20 A.
Channels 32..55 are exactly branches 8..13, i.e. slots 4, 5 and 6. Confirm
physically before concluding anything; nobody has looked in the crate.

**Slot occupancy, confirmed from a full 64-channel read.**

| slots | branches | voltage inputs | current inputs | reading |
|-------|----------|----------------|----------------|---------|
| 0,1,2,3,7 | 0-7, 14-15 | 11.8-12.8 V | at the 2.5 V zero (except below) | populated, powered |
| 4,5,6 | 8-13 | 76-152 uV (~0) | 264000-409000 uV (0.26-0.41 V) | **empty** |

The two input types fail differently and that is what makes the call solid: a
branch voltage input sits behind a 100:1 divider to ground, so with no rail
present it reads ~0. A current-sense input is high impedance and simply floats
when no sensor is attached, landing at a few hundred mV and drifting from
channel to channel (the twelve empty-slot current channels span 0.26-0.41 V and
no two agree). An absent module gives exactly this pair of signatures. Contrast
slot 7, physically the far end of the crate, which reads a healthy 12.47/12.51 V
- so this is not "the scan stops after slot 3".

**Branch 1 (slot 0, position B) AD rail is faulty.** Sampled every 4 s through
TPDO3:

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

Everything else is steady to +/-0.01 V; branch 1's AD rail swings between
**5.99 V and 11.70 V**. The user independently reported the "channel B" Va/d LED
flickering on the first module, which is this same branch. Two independent
observations of one fault. Note the config uses `syncIntervalMs="10000"`, so
these samples are aliased - the real oscillation is faster than 0.1 Hz. Drop
syncIntervalMs to ~1000 to see its actual shape.

**Branches 6 and 7 (slot 3): three of four current sensors read as floating.**

| channel | role | raw uV | reading |
|---------|------|--------|---------|
| ch28 | br6 CAN I | 2496299 | -0.030 A, sensor correctly at its 2.5 V zero |
| ch29 | br6 AD I  | 243915  | 0.244 V - floating |
| ch30 | br7 CAN I | 174029  | 0.174 V - floating |
| ch31 | br7 AD I  | 3309910 | 3.310 V, i.e. +6.5 A on an unloaded rail |

ch29 and ch30 sit in the same 0.17-0.24 V band as the twelve current inputs of
the confirmed-empty slots, i.e. they look disconnected rather than wrong. All
four of slot 3's *voltage* channels are healthy (11.86-12.43 V), and one of its
four current channels is perfect. That pattern - some contacts good, some
open - points at a **partially seated module or a damaged sense harness**, not
at the output stage. Reseat slot 3 and re-read before concluding anything.

**The RPDO cache trap (hit for real on 2026-08-25, then fixed).** Switching a
single branch off reported:

```
  DO word 0xFFFF -> 0xFFFE   (method: rpdo)
  read back    0x0000
  *** READ-BACK MISMATCH ***
```

Cause, confirmed by reading the server's own nodes: `do_word()` reads the ELMB's
real latch (SDO 0x6200), which was `0xFFFF` from `doInitHigh`. The write went to
`RPDO1.branch00`, a `RpdoCachedVariable`, which read-modify-writes the
**server's** 8-byte shadow cache and transmits all of it
(`DRpdoCachedVariable.cpp` `writeValue` -> `propagateCache`). That cache is
initialised to zeros (`DRpdo.cpp` `m_cache.assign(8, 0)`) and had never
transmitted, so it had no idea the crate had come up at `0xFFFF`. Clearing bit 0
of `0x0000` transmits `0x0000` and switches **all sixteen** branches off.

The read-back check caught it, which is exactly what it is there for. Afterwards
cache and crate are in sync, which is why the following `on 0` matched cleanly.

Fixed in `elmbpsu_opcua.py`: `--method rpdo` now writes the whole word through
`RPDO1.do_write` instead of the per-branch Booleans, so the cache is overwritten
wholesale and cannot diverge from intent. Verified with a no-op write of the
already-latched value (`word 0x0001` -> read back `0x0001`), so no hardware state
was changed to test it. The `branchNN` nodes stay in the address space for
clients that track their own state. `elmbpsu_can.py` was never affected — it
builds the full word itself and transmits its own RPDO.

**Side effect on the crate:** the incident left only branch 0 on. Branches 1-7,
14 and 15 had been on since power-up and are now off. `on all` restores them.

**What the DO bit actually does to the hardware.** Measured, from the switch-off
of branch 0:

| | branch 0 switched OFF | slot 4 module ABSENT |
|---|---|---|
| voltage sense | 0.008 V | 0.015 V |
| current sense | 0.012 A / 0.027 A, i.e. sensor at its 2.5 V zero | -19.82 A, i.e. sensor at ~0.02 V |

Two things follow. The branch output is **genuinely dead**, indistinguishable at
the sense point from an empty slot - not merely isolated behind a relay with a
live rail on the far side. And the module's own monitoring electronics **stay
powered**: the current sensor holds its 2.5 V zero point, which it could not do
if it were fed from the branch rail it is measuring. So the bit switches the
branch output, not the module.

The framework agrees on intent: `fwElmbPSU_hardReset()`
(`fwElmbPSU.ctl:1500`) is implemented as switch power off -> `delay(1)` ->
switch power on, and is the official way to cold-boot the ELMBs sitting on a
branch. CERN treats this as real removal of power and as a routine operation.

**Not established, and not establishable from software:** whether the DO line
drives the TRACO converter's Remote On/Off (inhibit) pin or a series switch in
its output. Inhibit is much the more likely - it is a standard TRACO control
input, it explains one bit switching both rails, and the alternative would leave
sixteen converters idling unloaded. But nothing measured here proves it. It is a
thirty-second question with a module in hand: look for a backplane DO line
landing on the converter's Remote On/Off pin versus a series FET/relay in the
output path, or check whether the converter stays cold with its branch off.

**The Burndy pinout is still unknown and is not in any of the material here.**
Searched `fwElmbPSU/`, `fwElmb/` and the PDF: the only hit is the phrase
"'burndy' connector" in the introduction, and `fwElmbPSUBurndyRef.pnl` is a UI
symbol carrying a right-click switch on/off menu, with no pin labels.
`fwElmbPSU_burndy.bmp` remains a 47x46 px icon. It is not recoverable from this
workspace - it needs EDMS (EDA-04145-V1-0) or the PH-ESS hardware page.

**You do not need the pinout to find the rails.** Use the crate as its own
signal generator: put the meter across a candidate pin pair, switch that branch
off in software, and watch. Only the pair belonging to that branch changes.
Details in README.md section 6.

**`mon --source sdo` does not work on this crate.** The on-request analog reads
at 0x2404 return `Bad` through the server (`aisdo_0`, `aisdo_1` both fail) while
ordinary SDO reads on the same node are fine (`do_C_read` = 255, `stateAsText` =
OPERATIONAL). The ADC itself is configured and scanning: `channelMax` = 64,
`range` = 4, `mode` = 1, `aiTransmissionType` = 1, and TPDO3 delivers. The
default for `mon` has been changed to `tpdo` accordingly; `sdo` is kept as the
fallback for a crate whose `aiTransmissionType` is not 1.

**Still unverified:** anything involving the OPC-UA server against the real
crate. At the time of writing the server had only ever polled node 63 and so
never exchanged a single frame with the crate.

## 8. What to do next / open items

1. **Wire the crate's control bus to a free CAN port and bring it up.** The
   adapter is there now (section 6); what is still unknown is whether the crate
   is physically connected to any of the free ports. Run `./can_diag.py` to pick
   one, then bring it up at 125000.
2. Run `./elmbpsu_can.py --iface <free-port> scan` — it confirms the bitrate,
   the wiring and the actual node id in one shot. If empty, retry the link at
   250000 and 50000 before suspecting wiring. Do **not** run it against `can9`
   or any port `can_diag.py` reports as IN USE: a second CANopen master on a
   live bus collides on SDO transfers.
3. Only then bother with the OPC-UA server. The raw tool can prove the hardware
   works; the server is for the production control path.
4. **Unknowns that hardware will settle immediately:** whether this crate is
   old-style (needs `--invert`); whether `dioOutputMaskC/A` are already `0xFF`;
   whether RPDO1 byte 0 really is port C (§4.7); the actual node id and bitrate.
5. Not yet investigated: the **Burndy connector pinout**. It is not in
   `ElmbPsuIntroduction.pdf` (which the ChatGPT session also noted), and
   `fwElmbPSU/pictures/fwElmbPSU_burndy.bmp` is a 47×46 px UI icon with no pin
   information. The user was told he would need the EDMS hardware record
   (EDA-04145-V1-0) or module photos for that. `fwInstallation-9.3.1/` and
   `jcop-framework-9.3.0.1/` have **not** been examined — the user asked that
   they only be touched as a last resort, and it never became necessary.
6. The user has been told he can have the README published as a shareable
   Artifact page; he has not asked for it.

---

## 9. Working style notes for the next agent

- The user is hands-on with the hardware and wants **commands he can run**, not
  theory. When he asked for a guide he specified: minimal text, 1–5 sentences
  per step followed by the command(s).
- He asked for reverse-engineering of `fwElmb`/`fwElmbPSU` rather than
  installing them. Keep citing the source file for any protocol claim.
- `fwInstallation-9.3.1/` and `jcop-framework-9.3.0.1/` are off-limits unless
  out of options or highly certain they contain something needed.
- Be straight about what is verified versus simulated. Nothing in this project
  has touched real hardware yet, and that has been stated plainly every time.
