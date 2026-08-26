# ELMB PSU — verified reference

Settled knowledge: the protocol, the mappings, the hardware behaviour, and the
quirks of the CanOpenOpcUa server. Everything here is either derived from a
named source file or measured on the crate, and none of it is expected to change.

**Live state — this crate, this machine, what is still open — is in
[HANDOFF.md](HANDOFF.md).** Operator procedure is in
[QUICKSTART.md](QUICKSTART.md).

Sources are the three read-only repositories `setup.sh` clones into the repo
root: `fwElmbPSU/`, `fwElmb/`, `CanOpenOpcUa/`, plus
`fwElmbPSU/source/ElmbPsuIntroduction.pdf`.

---

## 1. The crate

Up to 8 modules in 8 slots, 2 branches per module, 16 branches total.

Each branch's Burndy connector carries **two independent 12 V rails** — a *CAN*
rail (~25 W) and an *AD* (analog/digital) rail (~35 W). They are monitored
separately but switched together by **one** control bit.

**Both rails float.** Measured against chassis they read nothing. Measure each
positive against **its own return**. This one fact accounts for the entire
"every output reads 0 V" report that started this project.

Branch outputs are **not** enabled by mains power. One control ELMB inside the
crate drives the on/off switch of all 16 branches from its digital output ports
A and C (`ElmbPsuIntroduction.pdf` §2). At power-up those outputs take the state
stored in ELMB object **0x2300 `doInitHigh`**, which on an unconfigured crate
leaves every branch off. The front-panel LEDs indicate module/AC status, not
12 V presence on the Burndy.

The 120 Ω resistors in the ELMB/CAN network are CAN-line terminators only. They
have nothing to do with enabling an output, and no load is required.

### Branch numbering

`ElmbPsuIntroduction.pdf` Figure 4 (verified by rendering page 7 and reading the
image, not from prose):

```
        slot 0   slot 1   slot 2   slot 3   slot 4   slot 5   slot 6   slot 7
pos A     0        2        4        6        8       10       12       14
pos B     1        3        5        7        9       11       13       15

branch = 2*slot + (0 for position A/top, 1 for position B/bottom)
```

### The control bus

| item | value | source |
|------|-------|--------|
| bitrate | **125 kbit/s** default | `ElmbPsuIntroduction.pdf` §2 |
| control ELMB node id | **63 (0x3F)** default only — see below | `ElmbPsuIntroduction.pdf` §2 |
| control bus terminators | **none built in** — fit 120 Ω at both ends yourself | `ElmbPsuIntroduction.pdf`, "Terminators" |
| branch bus terminators | built into the modules, jumpers ST1/ST2 | same |
| DE-9 pinout (CiA-303) | pin 2 = CAN-L, 3 = CAN GND, 7 = CAN-H, 9 = optional V+ | CiA-303 |

**Node 63 is a default, not a guarantee — always confirm with a bus scan.**
Standard ELMB firmware sets the node id from 6 bits (0–63, and 63 is all-ones,
i.e. the unprogrammed state), so any crate that once shared a bus will have been
changed. `fwElmb/fwElmb_Readme.txt:110` lists "Can now choose node IDs greater
than 63 (for custom ELMB firmware)" as a feature. Nothing in the framework
hardcodes 63. The crate on this bench is **57**.

The control bus is electrically and logically separate from the powered
branches and needs its own CAN interface port on the PC.

**Back-plane isolation switch.** The crate has a switch on its back-plane that
connects or isolates the internal ELMB's supply from the control CAN bus
(`ElmbPsuIntroduction.pdf` §2). With more than one crate on a bus it **must** be
set to Isolated. On a single-crate bench either position can work; it decides
whether the crate feeds power onto the control bus, so check it before assuming
the CAN interface must supply V+ on DE-9 pin 9.

### External references not held here

- PH-ESS hardware page: <http://ess.web.cern.ch/ESS/canpsuProject/index.htm> —
  the most likely public source for the **Burndy pinout**, which is not in any
  material in this workspace.
- Branch connection scheme, EDMS:
  <https://edms.cern.ch/file/685351//CANbus_Guideline.pdf>
- fwElmbPSU / fwElmb downloads:
  <http://atlas.web.cern.ch/Atlas/GROUPS/DAQTRIG/DCS/ELMB/DIST/ELMBdoc.html>
- The Burndy pinout itself: EDMS record **EDA-04145-V1-0**.

---

## 2. Branch → digital output bit — the key mapping

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

That word goes to **RPDO1** (COB-ID `0x200 + node`, **byte 0 = port C, byte 1 =
port A**), which requires the node to be OPERATIONAL. It reads back from
**SDO 0x6200:01** (port C) and **0x6200:02** (port A).

### The one residual ambiguity

`fwElmbUser_createOPCFile()` in `fwElmbUser.ctl` — a **legacy** generator for
the old Slava OPC-DA server — labels RPDO1 byte 0 as **PORTF**, not PORTC. The
current maintained model (`OPCUA_nodeType_ELMB.xml`, the `.xmle` fragments, and
the read-back logic in `setDoBits`, which compares written against read and logs
"INCOHERENT" on mismatch) all say byte 0 = port C. We went with C/A.

Made harmless by design: every switch in both tools reads `0x6200` back and
reports a mismatch, and `--method sdo` bypasses the RPDO entirely by writing
each bit through its own object-dictionary entry. **If exactly the wrong half of
the branches respond, this is the cause.**

---

## 3. CANopen objects on the control ELMB

From `fwElmb/config/fwElmb/OPCUA_nodeType_ELMB.xml` and the
`CanOpenOpcUa/bin/CANopen_def_STDELMB_*.xmle` fragments:

| object | meaning |
|--------|---------|
| RPDO1, COB-ID `0x200 + node` | `do_write`, UInt16 at byte offset 0. Byte 0 = port C, byte 1 = port A. The path fwElmbPSU actually uses. Needs OPERATIONAL. |
| SDO `0x6200:01` / `:02` | `do_C_read` / `do_A_read` — read-back of the output latches |
| SDO `0x6220:01..08` | `do_C_0..7` bitwise write (branches 0–7) |
| SDO `0x6220:09..16` | `do_A_0..7` bitwise write (branches 8–15) — so sub-index = branch + 1 uniformly |
| SDO `0x6208:01` / `:02` | `dioOutputMaskC` / `dioOutputMaskA`, 1 = pin is an output. Must be `0xFF`/`0xFF` or nothing can switch. |
| SDO `0x2300:00` | `doInitHigh` — output state at ELMB power-up |
| SDO `0x2404:01..0x40` | `aisdo_0..63`, on-request analog reads, Int32 |
| TPDO3, COB-ID `0x380 + node` | multiplexed 64-channel analog scan, SYNC-driven, needs `aiTransmit.aiTransmissionType == 1` (SDO `0x1802:02`) |
| SDO `0x2100:01..04` | `channelMax`, `rate`, `range`, `mode` (ADC config) |
| SDO `0x1009`, `0x100A:00/:01`, `0x3100` | hwVersion, swVersion / swMinorVersion, serialNumber |
| SDO `0x1010:01` | `save` — persist parameters to EEPROM (write ASCII `"save"` as UInt32) |

### Polarity

`fwElmbPSU/scripts/libs/fwElmbPSU/fwElmbPSUConstants.ctl`:

```
// This is for new version of PSU
const bool EPSU_POWER_ON_VALUE = 1;
const bool EPSU_POWER_OFF_VALUE = 0;
// This is for old version of PSU
//const bool EPSU_POWER_ON_VALUE = 0;
//const bool EPSU_POWER_OFF_VALUE = 1;
```

Production ("new") crates: **1 = ON**. Pre-2.0.0 ("old") crates: inverted.
`fwElmbPSU_Readme.txt` states fwElmbPSU ≥ 2.0.0 supports production crates only.
Both tools default to production polarity and accept `--invert`.

---

## 4. Monitoring channels and unit conversion

`fwElmbPSU_createMonitorChannel()` computes, for branch *b*, the base channel
`ch = 4*b − 2*(b mod 2)`:

| quantity | ELMB analog input |
|----------|-------------------|
| CAN voltage | `ch` |
| AD voltage | `ch + 1` |
| CAN current | `ch + 4` |
| AD current | `ch + 5` |

That tiles all 64 ELMB inputs exactly once, 8 per module — verified in
`tests/selftest.py`. Raw values are **signed microvolts at the ADC input**. The
sensor formulas installed by `fwElmbPSU/scripts/fwElmbPSU/fwElmbPSU.postInstall`
are `"%c1*%x1/1000000.0"` with x1 = 100.0, and `"((%c1/1000000.0)-%x1)*%x2/%x3"`
with x1 = 2.5, x2 = 5.0, x3 = 0.625:

```
voltage [V] = raw/1e6 * 100.0                 (100:1 divider on the module)
current [A] = (raw/1e6 - 2.5) * 5.0 / 0.625   (2.5 V-centred sensor, 8 A/V)
```

Nominal per branch, from `fwElmbPSUConstants.ctl`: 12.0 V on both rails, 20 mA
CAN / 25 mA AD unloaded.

### Reading the monitoring values — what each pattern means

The voltage and current inputs fail *differently*, and that is what makes a
diagnosis from software solid. A branch voltage input sits behind a 100:1
divider to ground, so with no rail present it reads ~0. A current-sense input is
high impedance and simply floats when no sensor is attached, landing at a few
hundred mV and drifting from channel to channel.

| pattern | means |
|---------|-------|
| ~12 V on both rails, current at the 2.5 V zero (≈0 A) | branch populated, on, healthy |
| **V ≈ 0.01 V and I ≈ −19.8 A** | **module absent.** 0 V through the current formula gives (0 − 2.5) × 8 = −20 A |
| V healthy, but I sits at 0.17–0.41 V (reads as a nonsense current) | **that current-sense line is floating** — partially seated module or damaged sense harness, not an output fault |
| V ≈ 0.008 V, but I still at its 2.5 V zero | **branch switched OFF.** The output is dead; the module's own monitoring electronics are still powered |

The last two rows are the useful ones: an off branch and an absent module look
identical on the voltage channel and are told apart entirely by the current
channel.

### What the DO bit does to the hardware

Measured, comparing a branch switched off against a genuinely empty slot:

| | branch switched OFF | module ABSENT |
|---|---|---|
| voltage sense | 0.008 V | 0.015 V |
| current sense | 0.012 A / 0.027 A — sensor at its 2.5 V zero | −19.82 A — sensor at ~0.02 V |

Two conclusions. The branch output is **genuinely dead**, indistinguishable at
the sense point from an empty slot — not merely isolated behind a relay with a
live rail beyond it. And the module's monitoring electronics **stay powered**:
the current sensor holds its 2.5 V zero, which it could not do if it were fed
from the rail it measures. **The bit switches the branch output, not the module.**

The framework agrees on intent: `fwElmbPSU_hardReset()` (`fwElmbPSU.ctl:1500`)
is switch power off → `delay(1)` → switch power on, and is the official way to
cold-boot ELMBs sitting on a branch. CERN treats this as real removal of power
and as a routine operation.

### Which side of the connector the sensors are on

**The current sensors and their 2.5 V references are on the MODULE, not on the
crate backplane.** This decides how every faulty reading is read, so it is worth
the three lines of argument. From the two measurements above:

1. With a module removed, all four of that slot's current-sense channels float
   at 0.17–0.41 V, no two alike — the signature of a high-impedance ADC input
   that nothing is driving.
2. With the module in but its branch switched **off**, the same channels sit
   parked at 2.5 V.

A sensor living on the crate side would be fed from crate housekeeping power and
would hold its 2.5 V reference in *both* cases, reading zero current into an
empty slot. It does not. So the thing generating that signal leaves with the
module, and the analog line crosses the module-to-backplane connector. The ELMB
and its ADC are of course in the crate; only the sensing is not.

Strictly, this proves the *source* of the signal is on the module side of the
connector, not which PCB the sensor chip is soldered to — a backplane sensor
powered solely through a module-supplied rail would look identical. That is a
contrived design and nothing suggests it, but it has not been ruled out.

The reading rules that follow, per slot (a module spans branches 2s and 2s+1):

| what the slot's four current channels do | means |
|---|---|
| all four float, all four voltages ~0 | **no module**, or one making no contact at all |
| **some** float, others hold 2.5 V | module is there and partly working, so this is a **contact** problem — reseat it, then re-run |
| all four hold 2.5 V, a rail stays down | contact is fine; the fault is the module's **converter or output stage** |

A partial pattern can never be a dead module: a dead module could not produce
the good channels. To separate a bad module from a bad slot after reseating,
move that module to a slot this scan called healthy — the fault following the
module means the module, staying with the slot means the crate backplane.
`tests/crate_scan.py` applies exactly this ladder.

**Not established, and not establishable from software:** whether the DO line
drives the TRACO converter's Remote On/Off (inhibit) pin or a series switch in
its output. Inhibit is much the more likely — it is a standard TRACO control
input, it explains one bit switching both rails, and the alternative leaves
sixteen converters idling unloaded. Nothing measured proves it. With a module in
hand it is a thirty-second question: look for a backplane DO line landing on the
converter's Remote On/Off pin versus a series FET/relay in the output path, or
check whether the converter stays cold with its branch off.

---

## 5. Finding the rails on the Burndy without a pinout

The pinout is not in this workspace — the only hit across `fwElmbPSU/`,
`fwElmb/` and the PDF is the phrase "'burndy' connector";
`fwElmbPSUBurndyRef.pnl` is a UI symbol with a right-click on/off menu and no
pin labels, and `fwElmbPSU_burndy.bmp` is a 47×46 px icon. It needs EDMS
(EDA-04145-V1-0) or the PH-ESS hardware page. **You do not need it.**

Use the crate as its own signal generator. Both rails float and are isolated
from each other and from chassis, so probe **pin to pin, never pin to chassis**,
then let the software tell you which pair you found:

```bash
elmbpsu-opcua mon --branches 0     # confirm it is on and what the crate delivers
elmbpsu-opcua off 0                # meter across a candidate pin pair, then this
elmbpsu-opcua on 0                 # whatever dropped to 0 V is branch 0
```

Sweep efficiently: hold one probe on a fixed pin, walk the other across every
remaining pin, then move the fixed probe on by one. Each branch has four power
contacts — CAN +12 V and its return, AD +12 V and its return — plus the branch
CAN bus signals, so you are looking for two independent ~12 V pairs.

Two practical traps: the crate-side Burndy contacts are usually recessed and a
standard probe tip may not reach them — use a mating connector or a fine tip.
And check the front-panel CAN/AD LEDs first; they tell you which branches are
live before you probe anything.

---

## 6. The CanOpenOpcUa server

### It must run with privileges

CanModule's socketcan vendor **always** enters `CanVendorSocketCan.cpp:49
"Configuring SocketCAN device"` and shells out to `ip` to stop the link, set the
bitrate and restart it. Verified 2026-08-25 against `can13` on both installed
builds:

| build | settings | result |
|-------|----------|--------|
| v0.10.1 | `125k` / `DontConfigure` | `RTNETLINK answers: Operation not permitted` on both stop and bitrate → `Failed to open CAN device: SOCKET_ERROR` |
| v0.10.2 | `DontConfigure` | no stop attempt; bitrate 0 → `RTNETLINK answers: Invalid argument`, same failure, and **the port is left DOWN** |

Once the device fails to open, every frame gives `Failed to send CAN frame:
error code UNKNOWN_SEND_ERROR` and the startup SDO read times out (`SW Version
?.?` in the node table). **The OPC-UA endpoint still opens normally, which makes
the failure easy to miss.**

`--force_dont_reconfigure` does **not** help: it logs `note: forcing
DontReconfigure mode as per command line args` (`DBus.cpp:107`) and then does
nothing, because `settings = "Unspecified";` on the next line is **commented
out** (`Device/src/DBus.cpp:109`). `DontConfigure` maps to bitrate 0
(`DBus.cpp:390`), which CanModule does not treat as "skip". `--map_to_vcan` is
the only thing that sets `vcan=true`, and it rewrites the port name to
`vcan<N>`, so it is useless for real hardware.

**Therefore: `settings="125k"` plus `sudo`, or `setcap cap_net_admin+ep` on a
private copy of the binary** (`/home` is not `nosuid`, so file capabilities
work). This is exactly what the lab's own labTempMonitor does: `settings="125k"`,
running as root.

`lib/elmbpsu_can.py` needs none of this — it binds an `AF_CAN` socket and never
touches link configuration, so it runs as an ordinary user. It does require the
link to already be up.

### `--opcua_backend_config` is mandatory in practice

It defaults to `<directory of the binary>/ServerConfig.xml` via
`getApplicationPath()`, **not** the CWD (`BaseQuasarServer.cpp:308`; `--help`
prints the resolved path). See [../config/README.md](../config/README.md).

### Command line

Checked in `Server/src/BaseQuasarServer.cpp`: `--config_file` (also accepted
positionally), `--opcua_backend_config`, `--create_certificate`, `--help/-h`,
`--version/-v`, `--version_extra`.

Project extras from `Server/src/QuasarServer.cpp`: `--l<Component> <LEVEL>` where
Component ∈ {CanModule, Emergency, NodeMgmt, Rpdo, Sdo, SdoValidator, Spooky,
Spy, NmTpdo, MTpdo} and LEVEL ∈ {ERR,WRN,INF,DBG,TRC}; plus `--Wall`, `--Wnone`,
`--W<warning>`, `--Wno_<warning>`, `--force_dont_reconfigure`, `--map_to_vcan`,
`--print_cobids_tables`.

**There is no `-c` short option.**

### Lifecycle

Starts fine under `nohup ... &`, binds the endpoint, and **exits cleanly on plain
SIGTERM** (`kill <pid>`) as well as SIGINT. `systemd-run --user` also works, but
this account cannot read the user journal (and `Linger=no`), so redirect stdout
and stderr to a file regardless of method. Security is `None` with
`EnableAnonymous=true`, and the PKI paths in `ServerConfig.xml`
(`/localdisk/tmp/PKI/CA/certs/`) exist and are world-writable, so there is no
certificate work to do.

### The RPDO cache trap

**`RPDO1.branchNN` is a quasar `RpdoCachedVariable`. Writing one
read-modify-writes the *server's* 8-byte shadow cache and transmits the whole
thing** (`Device/src/DRpdoCachedVariable.cpp` `writeValue` → `propagateCache`).
That cache is initialised to zeros (`DRpdo.cpp` `m_cache.assign(8, 0)`) and knows
nothing about the state the crate powered up in from `doInitHigh`.

So the first per-branch write after a server restart transmits zeros and
**switches every branch off**, no matter which single branch you asked for. Hit
for real on 2026-08-25:

```
  DO word 0xFFFF -> 0xFFFE   (method: rpdo)
  read back    0x0000
  *** READ-BACK MISMATCH ***
```

The read-back check caught it, which is exactly what it is there for. Afterwards
cache and crate are in sync, so re-issuing the command works.

`lib/elmbpsu_opcua.py` now writes the full 16-bit word through `RPDO1.do_write`
for `--method rpdo`, so the cache is overwritten wholesale and cannot diverge
from intent. The `branchNN` nodes stay in the address space for clients that
track their own state. `lib/elmbpsu_can.py` was never affected — it builds the
word itself and transmits its own RPDO.

### The endpoint opening is not the crate answering

A freshly started server publishes its whole address space immediately but has
no *data* in it. Every read returns **`BadWaitingForInitialData`** until the
server has fetched that particular value from the ELMB once. For `stateAsText`
that means waiting on a node-guard cycle — `Bus/@nodeGuardIntervalMs`, 10 s
here — and for the `TPDO3.chNN.value` nodes it means waiting for the first SYNC,
`Bus/@syncIntervalMs`, also 10 s. So `Opened endpoint` in the log can precede a
usable read by ten seconds or more.

Hit for real on 2026-08-26: `tests/crate_scan.py` read `stateAsText` as soon as
`OpcUaServer.wait_ready()` returned and died with `BadWaitingForInitialData`.
`OpcUaServer.wait_ready()` only proves the endpoint is up; `PsuCrate.wait_ready()`
polls until the crate itself answers, and is what a client should use before its
first read. `tests/smoke_test.py` had been getting away with it only because its
bus scan happens to take a few seconds first.

### Other quasar / CanOpenOpcUa behaviour worth knowing

- The RPDO always transmits **8 data bytes** (`DRpdo.cpp` `m_cache.assign(8,0)`)
  even though the ELMB DO mapping is 2 bytes. Stock behaviour, used in production.
- `Boolean` packs to exactly 1 byte for SDO writes (`Device/src/ValueMapper.cpp`).
- `SdoVariable` `dataType` has **no Int16**: Boolean, Byte, UInt16, UInt32, Int32,
  ByteString.
- Multiplexed TPDO channels are auto-named **`ch0`..`ch63`** with `id = chno`
  (`Server/src/ConfigurationProcessing.cxx`); the specimen element must literally
  be named `specimen`.
- quasar builds string node ids by dot-joining the hierarchy
  (`AddressSpace/src/ASNodeManager.cpp` `makeChildNodeId`). The namespace URI is
  `"OPCUASERVER"` (`NodeManagerBase("OPCUASERVER", ...)`), normally ns index 2 —
  `lib/elmbpsu_opcua.py` resolves it at runtime rather than assuming.
- quasar declares these nodes as `BaseDataType`, so an **untyped write is
  rejected**; the client writes explicitly-typed Variants.

### Building it

The checkout is **not buildable as-is** — the `CanModuleMain` and `LogIt`
submodule directories are empty:

```bash
cd CanOpenOpcUa && git submodule update --init --recursive
```

It further needs an OPC-UA backend (open62541-compat is the free one), Boost,
XSD/xerces and the quasar toolchain; `quasar.py` drives the build. If ATLAS DCS
RPMs are available, installing the prebuilt package is far less work.

---

## 7. Address space produced by our config

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

with `<bus>` = `Bus/@name` and `<node>` = `Node/@name` from
[../config/config-elmbpsu.xml](../config/config-elmbpsu.xml).
