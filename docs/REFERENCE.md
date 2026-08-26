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

Each branch carries **two independent 12 V rails** — a *CAN* rail (~25 W) and an
*AD* (analog/digital) rail (~35 W). Monitored separately, switched together by
**one** control bit. They leave the module on its rear-side connector and reach
the outside world through the crate's own harness (§5).

**Both rails float**, and read nothing against chassis. That is why hand-metering
is not the way to check an output — see §5 — and it accounts for the entire
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

- PH-ESS hardware page: <http://ess.web.cern.ch/ESS/canpsuProject/index.htm>;
  connector drawings are EDMS **EDA-04145-V1-0**. Neither is needed to operate
  the crate — see §5.
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
current [A] = (raw/1e6 - 2.5) * 5.0 / 0.625   (2.5 V zero, 5 A per 0.625 V)
```

Nominal per branch, from `fwElmbPSUConstants.ctl`: 12.0 V on both rails, 20 mA
CAN / 25 mA AD unloaded.

### The current transducer

Each branch current is measured by a **LEM HX 05-P/SP2**, a Hall-effect current
transducer **mounted in the module**, in series with the branch output through
its primary terminals (pins 5 → 6). An internal Hall element senses the magnetic
field of that current, and the measurement side is galvanically isolated from
the current path — LEM specify a 3 kV insulation test voltage — which is what
lets the ELMB read the current of a floating 12 V output without bonding its ADC
to it.

| | |
|---|---|
| nominal current | **5 A** |
| measurable range | approximately **±15 A** |
| output at zero current | **2.5 V** |

The datasheet numbers are exactly the framework's conversion constants: 2.5 V is
the zero, and `× 5.0/0.625` maps a 0.625 V departure from it to the 5 A nominal
current. So the transducer's whole ±15 A range spans **0.625 V to 4.375 V** at
the ADC pin, and that gives a hard plausibility test:

> **An ADC reading outside 0.625–4.375 V is not a current measurement.** The
> transducer cannot produce it, so the line is floating, not carrying 18 A.

Because the transducer is inside the module, an implausible current reading is a
**module** fault — a floating sense line means that module's transducer or its
connection is not delivering a signal, and no crate-side wiring can cause or fix
it. `tests/crate_scan.py` checks plausibility before stability for this reason:
an undriven line sits rock steady at a few hundred mV and would sail through a
stdev test.

### Reading the monitoring values — what each pattern means

The two input types fail *differently*, which is what makes a software diagnosis
solid. A voltage input sits behind a 100:1 divider to ground and reads ~0 with no
rail present. A current input is high impedance and floats when the transducer is
not driving it, landing at a few hundred mV — below the transducer's own floor.

| pattern | means |
|---------|-------|
| ~12 V on both rails, current at the 2.5 V zero (≈0 A) | branch populated, on, healthy |
| **V ≈ 0.01 V and I ≈ −19.8 A** | **module absent.** An undriven input near 0 V runs through the formula as (0 − 2.5) × 8 = −20 A, well outside the transducer's range |
| V healthy, but I sits at 0.17–0.41 V | **that current line is floating** — the module's transducer or its connection, not an output fault |
| V ≈ 0.008 V, but I still at its 2.5 V zero | **branch switched OFF.** The output is dead; the module's own monitoring electronics are still powered |

The last two rows are the useful ones: an off branch and an absent module are
identical on the voltage channel and are told apart entirely by the current
channel.

Per slot (a module spans branches 2s and 2s+1):

| the slot's four current channels | means |
|---|---|
| all four float, all four voltages ~0 | **no module**, or one making no contact at all |
| **some** float, others hold 2.5 V | the module is there and partly working — a partial contact or a failed transducer |
| all four hold 2.5 V, a rail stays down | sensing is fine; the fault is the module's **converter or output stage** |

A partial pattern can never be a dead module — a dead module could not produce
the good channels. `tests/crate_scan.py` applies exactly this ladder.

### What the DO bit does to the hardware

Comparing a branch switched off against an empty slot:

| | branch switched OFF | module ABSENT |
|---|---|---|
| voltage sense | 0.008 V | 0.015 V |
| current sense | sensor at its 2.5 V zero | ~0.02 V, i.e. undriven |

The branch output is **genuinely dead**, indistinguishable at the sense point
from an empty slot — not merely isolated behind a relay with a live rail beyond
it. And the module's monitoring electronics **stay powered**: the transducer
holds its 2.5 V zero, which it could not do if it were fed from the rail it
measures. **The bit switches the branch output, not the module.**

The framework agrees on intent: `fwElmbPSU_hardReset()` (`fwElmbPSU.ctl:1500`)
is switch off → `delay(1)` → switch on, the official way to cold-boot ELMBs
sitting on a branch. CERN treats this as real removal of power and as routine.

Still open: whether the DO line drives the TRACO converter's Remote On/Off
(inhibit) pin or a series switch in its output. Inhibit is much the more likely —
standard TRACO control input, explains one bit switching both rails, and the
alternative leaves sixteen converters idling unloaded — but nothing measured
proves it, and it changes nothing operationally.

---

## 5. Where the branch outputs go, and why you should not meter them

The two 12 V rails leave the module on its **rear-side connector**. The crate is
then supposed to route them to its rear-panel connectors and out through a large
multi-channel **radial connector**. That routing is crate cabling — it depends on
the harness having been made up correctly for this particular crate, and it is
not described by anything in this workspace or in the module documentation.

**So metering the outputs by hand is impractical**, and this is a deliberate
decision, not a gap to fill in later. You cannot tell which radial-connector pin
belongs to which branch without tracing the crate's own harness; both rails
float, so there is no chassis reference to probe against; and a wrong reading
tells you nothing about whether the module or the cabling is at fault.

**Use the readout instead.** The ELMB's ADC sits upstream of all that cabling,
reading the module's own voltage divider and current transducer, so
`tests/crate_scan.py` characterises the module itself regardless of how the crate
is wired downstream:

```bash
elmbpsu-cratescan -n 10 --json scan.json
elmbpsu-opcua mon --branches 0-3        # spot check
```

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

**And the crate answering is not the crate being ready.** After a crate power
cycle the ELMB boots into PRE-OPERATIONAL. The server drives it to
`Node/@requestedState` from its node-management cycle, so `stateAsText` can
report PRE-OPERATIONAL for a node-guard period or two before it reports
OPERATIONAL — and RPDO writes are only acted on in OPERATIONAL. That is why the
first scan after a power cycle aborted and an immediate re-run succeeded: by
then the node had got there. `PsuCrate.wait_ready(require="OPERATIONAL")` waits
for the state it needs instead of for any state at all.

### A fresh TPDO3 set is not a fresh ADC sample

Reading the analog cache the moment it refreshes is not the same as reading the
crate. Two lags sit between a switch command and a number you can trust:

1. **the rails' own rise and fall time** — a converter that has just been
   inhibited still has charged output capacitors;
2. **the ELMB's ADC**, which works through its 64 inputs at its own pace. The
   SYNC-driven TPDO3 set that arrives next carries whatever the ADC had, which
   can be values sampled *before* the switch — and can be a **mix**, some
   channels from before and some from after.

Observed on this crate on 2026-08-26, reading one scan about a second after each
switch: rails commanded **off** read 11.85–12.79 V, and rails commanded **on**
read 0.01–0.02 V in the switch scan while the repeat scans a few seconds later
read 9.5 V on the same rails. Channels of the same branch disagreed with each
other within one scan. Every module in the crate was reported as failing to
switch.

### How long one sweep takes

Measured on this crate: repeat scans taken **2 s apart were 82% bit-identical**,
so only 18% of the channels had been re-converted in between. A sweep therefore
comes round about every **2 / 0.18 ≈ 11 s** — which is also how long the switch
phases of that run took to reach the commanded state, one full sweep.

Everything `tests/crate_scan.py` waits for is built on that number
(`ELMB_SWEEP_S`, 12 s with a little margin):

| wait | why |
|---|---|
| `--settle-window` | a rail counts as settled once nothing has moved for one sweep. Any shorter and "nothing moved" only means "the channels the ADC happened to revisit did not move" — the rest are bit-identical because they were never re-converted |
| `--sample-interval` | repeat scans closer together than one sweep are largely the same conversions read twice, and a variance built from those is a fiction |
| `--settle-timeout` | four sweeps, then give up and mark **those rails** unjudged |

Two consecutive reads agreeing proves nothing here — both can be stale — which
is why the comparison is against a scan a full window old, not the previous one.
Rails still moving when the wait ends are tracked **per channel**, so one module
bleeding down slowly does not make every other module's verdict unjudgeable, and
a rail it could not judge is reported as unjudged rather than as a fault.

If a different crate or a different `syncIntervalMs` sweeps at another rate, the
report says so: it counts bit-identical consecutive readings and names the
`--sample-interval` that would fix it.

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
