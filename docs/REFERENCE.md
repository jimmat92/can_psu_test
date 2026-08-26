# ELMB PSU — verified reference

Settled knowledge: protocol, mappings, hardware behaviour, and the quirks of the
CanOpenOpcUa server. Every claim here is derived from a named source file or
measured on the crate.

**Live state — this crate, this machine, what is still open — is in
[HANDOFF.md](HANDOFF.md).** Operator procedure: [QUICKSTART.md](QUICKSTART.md).

Sources are the three read-only repositories `setup.sh` clones into the repo
root — `fwElmbPSU/`, `fwElmb/`, `CanOpenOpcUa/` — plus
`fwElmbPSU/source/ElmbPsuIntroduction.pdf`.

---

## 1. The crate

Up to 8 modules in 8 slots, 2 branches per module, 16 branches total.

Each branch's Burndy connector carries **two independent 12 V rails** — a *CAN*
rail (~25 W) and an *AD* (analog/digital) rail (~35 W). Monitored separately,
switched together by **one** control bit.

**Both rails float.** Measured against chassis they read nothing; measure each
positive against **its own return**. This one fact accounts for the entire
"every output reads 0 V" report that started this project.

Branch outputs are **not** enabled by mains power. One control ELMB inside the
crate drives all 16 branch switches from its digital output ports A and C
(`ElmbPsuIntroduction.pdf` §2), and at power-up those outputs take the state
stored in ELMB object **0x2300 `doInitHigh`** — on an unconfigured crate, every
branch off. The front-panel LEDs indicate module/AC status, not 12 V presence on
the Burndy. The 120 Ω resistors in the ELMB/CAN network are CAN-line terminators
only, nothing to do with enabling an output, and no load is required.

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

Electrically and logically separate from the powered branches; needs its own CAN
interface port on the PC.

| item | value | source |
|------|-------|--------|
| bitrate | **125 kbit/s** default | `ElmbPsuIntroduction.pdf` §2 |
| control ELMB node id | **63 (0x3F)** default only — see below | `ElmbPsuIntroduction.pdf` §2 |
| control bus terminators | **none built in** — fit 120 Ω at both ends yourself | `ElmbPsuIntroduction.pdf`, "Terminators" |
| branch bus terminators | built into the modules, jumpers ST1/ST2 | same |
| DE-9 pinout (CiA-303) | pin 2 = CAN-L, 3 = CAN GND, 7 = CAN-H, 9 = optional V+ | CiA-303 |

**Node 63 is a default, not a guarantee — always confirm with a bus scan.**
Standard ELMB firmware sets the node id from 6 bits (63 = all-ones, the
unprogrammed state), so any crate that once shared a bus will have been changed;
`fwElmb/fwElmb_Readme.txt:110` lists IDs greater than 63 as a custom-firmware
feature. Nothing in the framework hardcodes 63. This bench crate is **57**.

**Back-plane isolation switch.** Connects or isolates the internal ELMB's supply
from the control CAN bus (`ElmbPsuIntroduction.pdf` §2). With more than one crate
on a bus it **must** be Isolated. On a single-crate bench either position works —
it decides whether the crate feeds power onto the control bus, so check it before
assuming the CAN interface must supply V+ on DE-9 pin 9.

### External references not held here

- Burndy pinout: EDMS **EDA-04145-V1-0**, or the PH-ESS hardware page
  <http://ess.web.cern.ch/ESS/canpsuProject/index.htm>. It is in no material in
  this workspace.
- Branch connection scheme: <https://edms.cern.ch/file/685351//CANbus_Guideline.pdf>
- fwElmbPSU / fwElmb downloads:
  <http://atlas.web.cern.ch/Atlas/GROUPS/DAQTRIG/DCS/ELMB/DIST/ELMBdoc.html>

---

## 2. Branch → digital output bit — the key mapping

`fwElmbPSU_createPowerControl()` in
`fwElmbPSU/scripts/libs/fwElmbPSU/fwElmbPSU.ctl`:

```
if (argiBranchNumber > 7) { sPort = "A"; iBit = argiBranchNumber - 8; }
else                      { sPort = "C"; iBit = argiBranchNumber;     }
```

`fwElmbUser_setDoBits()` / `fwElmbUser_getDoBytes()` in
`fwElmb/scripts/libs/fwElmb/fwElmbUser.ctl` combine the ports into one 16-bit
word:

```
port A -> mask = 1 << (bit + 8)
port C -> mask = 1 << bit
getDoBytes: uResult = (uPortA << 8) | uPortC
```

> **Bit N of the 16-bit DO word is branch N**, for all 16 branches. No
> reordering, no surprises.

That word goes to **RPDO1** (COB-ID `0x200 + node`, **byte 0 = port C, byte 1 =
port A**), which requires the node to be OPERATIONAL. It reads back from
**SDO 0x6200:01** (port C) and **0x6200:02** (port A).

**The one residual ambiguity:** `fwElmbUser_createOPCFile()` — a **legacy**
generator for the old Slava OPC-DA server — labels RPDO1 byte 0 as **PORTF**.
The maintained model (`OPCUA_nodeType_ELMB.xml`, the `.xmle` fragments, and
`setDoBits`, which logs "INCOHERENT" on mismatch) all say port C. We went with
C/A, and made it harmless: every switch in both tools reads `0x6200` back and
reports a mismatch, and `--method sdo` bypasses the RPDO by writing each bit
through its own object-dictionary entry. **If exactly the wrong half of the
branches respond, this is the cause.**

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

**Polarity**, from `fwElmbPSU/scripts/libs/fwElmbPSU/fwElmbPSUConstants.ctl`:

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

`fwElmbPSU_createMonitorChannel()` computes, for branch *b*, base channel
`ch = 4*b − 2*(b mod 2)`: CAN voltage at `ch`, AD voltage at `ch+1`, CAN current
at `ch+4`, AD current at `ch+5`. That tiles all 64 ELMB inputs exactly once, 8
per module — verified in `tests/selftest.py`.

Raw values are **signed microvolts at the ADC input**. The sensor formulas
installed by `fwElmbPSU/scripts/fwElmbPSU/fwElmbPSU.postInstall` are
`"%c1*%x1/1000000.0"` with x1 = 100.0, and `"((%c1/1000000.0)-%x1)*%x2/%x3"`
with x1 = 2.5, x2 = 5.0, x3 = 0.625:

```
voltage [V] = raw/1e6 * 100.0                 (100:1 divider on the module)
current [A] = (raw/1e6 - 2.5) * 5.0 / 0.625   (2.5 V-centred sensor, 8 A/V)
```

Nominal per branch, from `fwElmbPSUConstants.ctl`: 12.0 V on both rails, 20 mA
CAN / 25 mA AD unloaded.

### Reading the monitoring values — what each pattern means

The two input types fail *differently*, and that is what makes a software
diagnosis solid. A voltage input sits behind a 100:1 divider to ground, so with
no rail present it reads ~0. A current-sense input is high impedance and floats
when nothing drives it, landing at a few hundred mV and drifting from channel to
channel.

| pattern | means |
|---------|-------|
| ~12 V on both rails, current at the 2.5 V zero (≈0 A) | branch populated, on, healthy |
| **V ≈ 0.01 V and I ≈ −19.8 A** | **module absent.** 0 V through the current formula gives (0 − 2.5) × 8 = −20 A |
| V healthy, but I sits at 0.17–0.41 V (nonsense current) | **that current-sense line is floating** — partially seated module or damaged sense harness, not an output fault |
| V ≈ 0.008 V, but I still at its 2.5 V zero | **branch switched OFF.** The output is dead; the module's own monitoring electronics are still powered |

The last two rows are the useful ones: an off branch and an absent module are
identical on the voltage channel and are told apart entirely by the current
channel.

### What the DO bit does to the hardware

Measured, comparing a branch switched off against a genuinely empty slot:

| | branch switched OFF | module ABSENT |
|---|---|---|
| voltage sense | 0.008 V | 0.015 V |
| current sense | 0.012 A / 0.027 A — sensor at its 2.5 V zero | −19.82 A — sensor at ~0.02 V |

The branch output is **genuinely dead**, indistinguishable at the sense point
from an empty slot — not merely isolated behind a relay with a live rail beyond
it. And the module's monitoring electronics **stay powered**: the current sensor
holds its 2.5 V zero, which it could not do if it were fed from the rail it
measures. **The bit switches the branch output, not the module.**

The framework agrees on intent: `fwElmbPSU_hardReset()` (`fwElmbPSU.ctl:1500`)
is switch off → `delay(1)` → switch on, the official way to cold-boot ELMBs
sitting on a branch. CERN treats this as real removal of power and as routine.

### Which side of the connector the sensors are on

**The current sensors and their 2.5 V references are on the MODULE, not the
crate backplane** — which decides how every faulty reading is read. From the two
measurements above: a removed module leaves that slot's four current-sense
channels floating at 0.17–0.41 V, no two alike, while a module present but
switched **off** parks them at 2.5 V. A crate-side sensor, fed from crate
housekeeping power, would hold its reference in *both* cases and read zero
current into an empty slot. It does not, so the signal source leaves with the
module and the analog line crosses the module-to-backplane connector. (Strictly
this locates the *source*, not which PCB the chip is soldered to: a backplane
sensor powered solely through a module-supplied rail would look identical.
Contrived, nothing suggests it, not ruled out.)

The reading rules that follow, per slot (a module spans branches 2s and 2s+1):

| the slot's four current channels | means |
|---|---|
| all four float, all four voltages ~0 | **no module**, or one making no contact at all |
| **some** float, others hold 2.5 V | module is there and partly working → a **contact** problem. Reseat, re-run. |
| all four hold 2.5 V, a rail stays down | contact is fine; the fault is the module's **converter or output stage** |

A partial pattern can never be a dead module — a dead module could not produce
the good channels. To separate a bad module from a bad slot after reseating, move
that module to a slot this scan called healthy: the fault following the module
means the module, staying with the slot means the crate backplane.
`tests/crate_scan.py` applies exactly this ladder.

**Not establishable from software:** whether the DO line drives the TRACO
converter's Remote On/Off (inhibit) pin or a series switch in its output.
Inhibit is much the more likely — standard TRACO control input, explains one bit
switching both rails, and the alternative leaves sixteen converters idling
unloaded — but nothing measured proves it. With a module in hand: look for a
backplane DO line landing on the converter's Remote On/Off pin versus a series
FET/relay in the output path, or check whether the converter stays cold with its
branch off.

---

## 5. Finding the rails on the Burndy without a pinout

The pinout is not in this workspace — the only hit across `fwElmbPSU/`,
`fwElmb/` and the PDF is the phrase "'burndy' connector"
(`fwElmbPSUBurndyRef.pnl` is a UI symbol with no pin labels,
`fwElmbPSU_burndy.bmp` a 47×46 px icon). **You do not need it.**

Both rails float and are isolated from each other and from chassis, so probe
**pin to pin, never pin to chassis**, and let the software tell you which pair
you found:

```bash
elmbpsu-opcua mon --branches 0     # confirm it is on and what the crate delivers
elmbpsu-opcua off 0                # meter across a candidate pin pair, then this
elmbpsu-opcua on 0                 # whatever dropped to 0 V is branch 0
```

Sweep by holding one probe on a fixed pin, walking the other across the rest,
then moving the fixed probe on by one. Each branch has four power contacts —
CAN +12 V and return, AD +12 V and return — plus the branch CAN signals, so you
want two independent ~12 V pairs.

Two traps: crate-side Burndy contacts are usually recessed and a standard probe
tip may not reach them, so use a mating connector or a fine tip; and check the
front-panel CAN/AD LEDs first, which tell you which branches are live before you
probe anything.

---

## 6. The CanOpenOpcUa server

### It must run with privileges

CanModule's socketcan vendor **always** enters `CanVendorSocketCan.cpp:49
"Configuring SocketCAN device"` and shells out to `ip` to stop the link, set the
bitrate and restart it. Verified 2026-08-25 against `can13`:

| build | settings | result |
|-------|----------|--------|
| v0.10.1 | `125k` / `DontConfigure` | `RTNETLINK answers: Operation not permitted` on both stop and bitrate → `Failed to open CAN device: SOCKET_ERROR` |
| v0.10.2 | `DontConfigure` | no stop attempt; bitrate 0 → `RTNETLINK answers: Invalid argument`, same failure, and **the port is left DOWN** |

Once the device fails to open, every frame gives `Failed to send CAN frame:
error code UNKNOWN_SEND_ERROR` and the startup SDO read times out (`SW Version
?.?` in the node table). **The OPC-UA endpoint still opens normally, which makes
this easy to miss.**

`--force_dont_reconfigure` does **not** help: it logs `note: forcing
DontReconfigure mode as per command line args` (`DBus.cpp:107`) then does
nothing, because `settings = "Unspecified";` on the next line is **commented
out** (`Device/src/DBus.cpp:109`). `DontConfigure` maps to bitrate 0
(`DBus.cpp:390`), which CanModule does not treat as "skip". `--map_to_vcan` is
the only thing that sets `vcan=true` and it rewrites the port name to `vcan<N>`,
so it is useless for real hardware.

**Therefore: `settings="125k"` plus `sudo`, or `setcap cap_net_admin+ep` on a
private copy of the binary** (`/home` is not `nosuid`, so file capabilities
work) — exactly what the lab's own labTempMonitor does.

`lib/elmbpsu_can.py` needs none of this: it binds an `AF_CAN` socket and never
touches link configuration. It does require the link to already be up.

### `--opcua_backend_config` is mandatory in practice

It defaults to `<directory of the binary>/ServerConfig.xml` via
`getApplicationPath()`, **not** the CWD (`BaseQuasarServer.cpp:308`; `--help`
prints the resolved path). See [../config/README.md](../config/README.md).

### Command line

From `Server/src/BaseQuasarServer.cpp`: `--config_file` (also positional),
`--opcua_backend_config`, `--create_certificate`, `--help/-h`, `--version/-v`,
`--version_extra`. **There is no `-c` short option.**

Project extras from `Server/src/QuasarServer.cpp`: `--l<Component> <LEVEL>` where
Component ∈ {CanModule, Emergency, NodeMgmt, Rpdo, Sdo, SdoValidator, Spooky,
Spy, NmTpdo, MTpdo} and LEVEL ∈ {ERR,WRN,INF,DBG,TRC}; plus `--Wall`, `--Wnone`,
`--W<warning>`, `--Wno_<warning>`, `--force_dont_reconfigure`, `--map_to_vcan`,
`--print_cobids_tables`.

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
**switches every branch off**, whichever single branch you asked for. Hit for
real on 2026-08-25:

```
  DO word 0xFFFF -> 0xFFFE   (method: rpdo)
  read back    0x0000
  *** READ-BACK MISMATCH ***
```

The read-back check caught it. Afterwards cache and crate are in sync, so
re-issuing the command works.

`lib/elmbpsu_opcua.py` now writes the full 16-bit word through `RPDO1.do_write`
for `--method rpdo`, so the cache is overwritten wholesale and cannot diverge
from intent; the `branchNN` nodes stay in the address space for clients that
track their own state. `lib/elmbpsu_can.py` was never affected — it builds the
word itself and transmits its own RPDO.

### The endpoint opening is not the crate answering

A freshly started server publishes its whole address space immediately but has no
*data* in it. Every read returns **`BadWaitingForInitialData`** until the server
has fetched that particular value from the ELMB once. For `stateAsText` that
means waiting on a node-guard cycle (`Bus/@nodeGuardIntervalMs`, 10 s here); for
`TPDO3.chNN.value` it means waiting for the first SYNC (`Bus/@syncIntervalMs`,
also 10 s). So `Opened endpoint` in the log can precede a usable read by ten
seconds or more.

Hit for real on 2026-08-26: `tests/crate_scan.py` read `stateAsText` as soon as
`OpcUaServer.wait_ready()` returned and died. `OpcUaServer.wait_ready()` only
proves the endpoint is up; **`PsuCrate.wait_ready()` polls until the crate itself
answers**, and is what a client should use before its first read.
`tests/smoke_test.py` had been getting away with it only because its bus scan
happens to take a few seconds first.

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

**Not buildable as-is** — the `CanModuleMain` and `LogIt` submodule directories
are empty (`cd CanOpenOpcUa && git submodule update --init --recursive`). It
further needs an OPC-UA backend (open62541-compat is the free one), Boost,
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
