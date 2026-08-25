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
/home/dmatakia/can_psu_test/                 <- primary working directory
├── ElmbPsuIntroduction.pdf                  reference: the CERN PSU guide (8 pages)
├── initial_discussion.txt                   reference: earlier ChatGPT session notes
├── CanOpenOpcUa/                            reference: the OPC-UA server source
├── fwElmb/                                  reference: JCOP ELMB component
├── fwElmbPSU/                               reference: JCOP ELMB PSU component
├── fwInstallation-9.3.1/                    reference: not needed so far
├── jcop-framework-9.3.0.1/                  reference: not needed so far
└── can_psu_test/                            <- OUR DELIVERABLES (git repo)
    ├── elmbpsu_can.py                       SocketCAN tool, zero dependencies
    ├── selftest.py                          offline verification of the above
    ├── config-elmbpsu.xml                   CanOpenOpcUa server config
    ├── elmbpsu_opcua.py                     OPC-UA client (the WinCC OA replacement)
    ├── README.md                            full write-up + troubleshooting
    ├── QUICKSTART.md                        short operator procedure
    └── HANDOFF.md                           this file
```

Note the nesting: the deliverables directory is `can_psu_test/can_psu_test/`.
The user renamed it from `psu_test/` after it was created. It is a git repo
(`origin = https://github.com/jimmat92/can_psu_test.git`, one commit
`42d0ad2 Initial commit`).

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
is the short operator procedure: bring up `can0` → start the server → CAN
debugging → status → switch → read.

---

## 6. Environment state — READ THIS BEFORE PROMISING ANYTHING

**There is no CAN hardware attached and nothing has been tested on real
hardware.** Everything above is verified against simulators only.

```
/sys/class/net/          -> eno1, eno2, lo      (no can0, no vcan)
lsusb                    -> no CAN adapter present
candump/cansend          -> not installed (no can-utils)
vcan kernel module       -> NOT present (would need kernel-modules-extra),
                            so not even a virtual-CAN dry run is possible
```

Available in-tree SocketCAN drivers: `peak_usb`, `kvaser_usb`, `gs_usb`,
`usb_8dev`, `slcan`, `peak_canfd`, plus `can.ko` / `can-raw.ko`. So a PEAK
PCAN-USB, Kvaser, or CANable/candleLight will enumerate as `can0` with no extra
work. A **SysTec USB-CANmodul needs SysTec's out-of-tree driver** (CERN ships
RPMs). An **AnaGate** needs no kernel driver but is *not* reachable by
`elmbpsu_can.py` — it would need `provider="an"` in the server config instead.

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
- Python **3.13.1** at `/usr/local/bin/python3`; `socket.AF_CAN` and
  `socket.CAN_RAW` both present.
- **`asyncua` 2.0.1 was pip-installed** into
  `/usr/local/lib/python3.13/site-packages/` during this work. `python-can` is
  not installed and is not needed.
- The server binary path assumed throughout is `/opt/CanOpenOpcUa/bin/` (from
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

## 8. What to do next / open items

1. **Get a CAN adapter onto the control bus.** Nothing further can be verified
   without one. This is the single blocking item.
2. Run `./elmbpsu_can.py --iface can0 scan` first — it confirms the bitrate,
   the wiring and the actual node id in one shot. If empty, retry the link at
   250000 and 50000 before suspecting wiring.
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
