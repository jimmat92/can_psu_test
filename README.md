# ELMB PSU crate — bring-up and control without WinCC OA

Everything here was derived from the sources in this workspace:
`ElmbPsuIntroduction.pdf`, `fwElmbPSU/`, `fwElmb/`, `CanOpenOpcUa/`.
Every mapping claim below cites the file it came from.

## 1. Why your outputs read 0 V

That is the expected state. Branch outputs are **not** enabled by mains power.

The crate contains one control ELMB whose digital output ports **A** and **C**
drive the on/off switch of the 16 branches (`ElmbPsuIntroduction.pdf` §2). At
power-up those outputs take the state stored in ELMB object **0x2300
`doInitHigh`** — which for a crate that has never been configured leaves every
branch off. The front-panel LEDs indicate module/AC status, not that 12 V is
present on the Burndy.

So no resistor, no load, and no ELMB on the branch is needed. You need to talk
to the control ELMB and set a bit.

Two more things worth knowing before you probe with a meter:

- Each branch's Burndy carries **two independent 12 V rails** — the *CAN* rail
  (~25 W) and the *AD* (analog/digital) rail (~35 W). They are monitored
  separately but switched together by **one** control bit. That matches your
  "2 outputs per CAN bus".
- The rails are floating. Measure each positive pin against **its own return**,
  not against chassis.

## 2. Check the machine before you touch anything

`pcaticstest08` is a **shared** machine. Other people run WinCC OA projects,
CanOpenOpcUa servers and CAN traffic on it, and a bus that is already carrying
a CANopen master is not yours to take. Run this first:

```bash
./can_diag.py
```

It reports, in three sections:

1. **OPC-UA servers running here.** Every listening TCP port is greeted with an
   OPC-UA `HELLO` (OPC 10000-6 §7.1.2); anything that answers `ACK` or `ERR` is
   an OPC-UA server, anything else is not. Confirmed servers are then asked
   `GetEndpoints` for their application name, so you see *which* server it is —
   `CanOpen@host` / `urn:CERN:CanOpenOpcUa` is another CanOpenOpcUa instance and
   very likely already owns a CAN port.
2. **What CAN interfaces exist**, with link state, CAN state, bitrate, frame
   counters and a live frame rate, grouped by the physical USB adapter behind
   them.
3. **Which are used and which are free.** "Used" comes from the kernel's own
   receive lists (`/proc/net/can/rcvlist_*`) — an entry there means a process
   holds an open socket bound to that device, and it covers every user on the
   box whether or not you can read their `/proc`. That is cross-checked against
   the live frame rate and, where possible, attributed to a PID via `lsof`.

It is read-only: it never configures a link and never transmits a CAN frame.
`--json` gives the same data machine-readably; `--no-probe` stops it connecting
to anything.

At the time of writing this host has a **SysTec Multiport CAN-to-USB**, four
boxes of two ports each, as `can8`…`can15` (`systec_can`, out-of-tree, already
installed). `can9` is busy with a live CANopen system. Do not assume that is
still true — that is what the script is for.

Once you have a free port, bring it up at the ELMB PSU default bitrate:

```bash
sudo ip link set can8 down
sudo ip link set can8 type can bitrate 125000
sudo ip link set can8 up
ip -details link show can8
```

Control-bus CAN pinout on the DE-9 (CiA-303 standard, which the ELMB follows):
pin 2 = CAN-L, pin 3 = CAN GND, pin 7 = CAN-H, pin 9 = optional V+.
**The control bus has no built-in terminators** (`ElmbPsuIntroduction.pdf`,
"Terminators") — you must fit 120 Ω at both ends yourself. The ST1/ST2 jumpers
inside the modules only terminate the *branch* buses.

## 3. Fastest path: talk to the ELMB directly

`elmbpsu_can.py` speaks CANopen straight over SocketCAN. No dependencies at all
(it uses CPython's built-in `AF_CAN`), so there is nothing to build and nothing
to install. Use this to answer "is the crate alive and can I switch a branch?"
before you invest in the OPC-UA stack.

Substitute the interface `can_diag.py` told you was free — `can8` below.

```bash
# 1. Is anything on the bus? (probes node-guard on all 127 node ids)
./elmbpsu_can.py --iface can8 scan

# 2. Who is node 63?
./elmbpsu_can.py --iface can8 info

# 3. Current configuration and branch states
./elmbpsu_can.py --iface can8 status

# 4. Switch slot 0 position A (branch 0) on, with automatic read-back verify
./elmbpsu_can.py --iface can8 on 0

# 5. Measure. Then read what the crate thinks the rails are doing:
./elmbpsu_can.py --iface can8 mon --branches 0

# 6. Everything on / everything off
./elmbpsu_can.py --iface can8 on all
./elmbpsu_can.py --iface can8 off all
```

Never point this at a bus `can_diag.py` reported as IN USE. Two CANopen
masters on one bus collide on SDO transfers, and you would be switching
branches out from under whoever else is on it.

Useful extras: `-v` traces every CAN frame, `dump` is a passive candump,
`nmt start|preop|reset` drives the NMT state machine, and `--method sdo` uses
bitwise SDO writes to 0x6220 instead of the RPDO (works in PRE-OPERATIONAL,
which is handy when you suspect the node never reached OPERATIONAL).

`selftest.py` exercises all of the above against a simulated ELMB — run it to
confirm the tool itself is sane before blaming the hardware.

## 4. The mapping, and where it comes from

### Branch numbering (`ElmbPsuIntroduction.pdf` Figure 4)

```
        slot 0   slot 1   slot 2   slot 3   slot 4   slot 5   slot 6   slot 7
pos A     0        2        4        6        8       10       12       14
pos B     1        3        5        7        9       11       13       15

branch = 2*slot + (0 for position A/top, 1 for position B/bottom)
```

### Branch → digital output bit

`fwElmbPSU_createPowerControl()` in `fwElmbPSU/scripts/libs/fwElmbPSU/fwElmbPSU.ctl`:

| branch | ELMB port | bit |
|--------|-----------|-----|
| 0 – 7  | C         | branch |
| 8 – 15 | A         | branch − 8 |

`fwElmbUser_setDoBits()` / `fwElmbUser_getDoBytes()` in `fwElmb/.../fwElmbUser.ctl`
assemble those two ports into one 16-bit word as `(portA << 8) | portC`, so:

> **bit N of the 16-bit DO word is branch N**, for all 16 branches.

That word is written to **RPDO1** (COB-ID `0x200 + node`, byte 0 = port C,
byte 1 = port A) — this is the exact path fwElmbPSU uses, and it requires the
node to be **OPERATIONAL**. It is read back with **SDO 0x6200:01** (port C) and
**0x6200:02** (port A) — see `fwElmb/config/fwElmb/OPCUA_nodeType_ELMB.xml`.

### Polarity (`fwElmbPSUConstants.ctl`)

```
production ("new") PSU:  1 = ON,  0 = OFF     <- the default in both tools
pre-2.0.0 ("old") PSU:   0 = ON,  1 = OFF     <- pass --invert
```

The framework readme is explicit that fwElmbPSU ≥ 2.0.0 only supports the
production crates. If your crate is an old one, everything still works, you just
need `--invert`.

### Monitoring channels (`fwElmbPSU_createMonitorChannel()`)

For branch *b*, with `ch = 4*b − 2*(b mod 2)`:

| quantity | ELMB analog input |
|----------|-------------------|
| CAN voltage | `ch` |
| AD voltage  | `ch + 1` |
| CAN current | `ch + 4` |
| AD current  | `ch + 5` |

which tiles all 64 ELMB inputs exactly once — 8 channels per module.
Raw values are **signed microvolts at the ADC input**. From the sensor
definitions installed by `fwElmbPSU/scripts/fwElmbPSU/fwElmbPSU.postInstall`:

```
voltage [V] = raw/1e6 * 100.0                    (100:1 divider on the module)
current [A] = (raw/1e6 - 2.5) * 5.0 / 0.625      (2.5 V-centred sensor, 8 A/V)
```

Nominal per branch, from `fwElmbPSUConstants.ctl`: 12.0 V on both rails,
20 mA on the CAN rail and 25 mA on the AD rail with no load.

## 5. The OPC-UA route

### The config file

`config-elmbpsu.xml` is a complete, self-contained CanOpenOpcUa configuration
for one crate. It deliberately does **not** use the `CANopen_def_STDELMB_*.xmle`
entity includes, so there is no DTD path to get wrong — the equivalent content
is inlined, plus the PSU-specific extras. Adjust `Bus/@port`, `Bus/@settings`
and `Node/@id` at the top of the file.

What it exposes beyond the stock ELMB model, and why:

| node | why it is there |
|------|-----------------|
| `RPDO1.branch00` … `branch15` | one Boolean per branch, so a client flips one branch without doing its own read-modify-write of the word |
| `RPDO1.do_write` | the raw 16-bit word, identical to what fwElmbPSU writes |
| `do_bitwise.do_C_0` … `do_A_7` | SDO 0x6220 bitwise writes; work in PRE-OPERATIONAL, good for diagnosis |
| `dioOutputMask.dioOutputMaskC/A` | SDO 0x6208 — if these are not `0xFF` the pins are inputs and no branch can ever switch |
| `doInitHigh` | SDO 0x2300 — the power-up state; set this to make the crate come up with branches already on |
| `aisdo.aisdo_0` … `63` | on-request analog reads, so monitoring works even if the ELMB is not set up for SYNC/TPDO3 |

Run it:

```bash
/opt/CanOpenOpcUa/bin/CanOpenOpcUa --config_file /path/to/config-elmbpsu.xml
```

Endpoint is `opc.tcp://<host>:48012`, anonymous, security `None`
(`CanOpenOpcUa/bin/ServerConfig.xml`).

### Building the server

The checkout in this workspace is **not buildable as-is** — the `CanModuleMain`
and `LogIt` submodules are empty directories:

```bash
cd CanOpenOpcUa
git submodule update --init --recursive
```

It also needs an OPC-UA backend (open62541-compat is the free one), Boost,
XSD/xerces and the quasar toolchain; `quasar.py` drives all of it. If ATLAS DCS
RPMs are available to you, installing the prebuilt `CanOpenOpcUa` package is far
less work than building from source.

### The client

`elmbpsu_opcua.py` is the WinCC OA replacement. `pip install asyncua`, then:

```bash
./elmbpsu_opcua.py --endpoint opc.tcp://localhost:48012 status
./elmbpsu_opcua.py --endpoint opc.tcp://localhost:48012 on 0
./elmbpsu_opcua.py --endpoint opc.tcp://localhost:48012 off all
./elmbpsu_opcua.py --endpoint opc.tcp://localhost:48012 mon --branches 0-3
./elmbpsu_opcua.py --endpoint opc.tcp://localhost:48012 browse --depth 2
```

It resolves the `OPCUASERVER` namespace index at runtime, writes correctly-typed
Variants (quasar declares these nodes as `BaseDataType`, so an untyped write is
rejected), and verifies every switch by reading 0x6200 back.

Both scripts were validated end-to-end against simulated devices — `selftest.py`
against a fake CANopen ELMB, and the OPC-UA client against a mock server
reproducing this exact address space.

## 6. If a branch still shows 0 V after a verified switch-on

Work down this list:

1. **Read-back mismatch reported by the tool.** The ELMB did not latch the
   write. Check `dioOutputMaskC`/`dioOutputMaskA` are `0xFF` (`status`); if not,
   `outmask 0xFF 0xFF --store` sets them and persists to EEPROM. Also confirm
   the node was OPERATIONAL, and try `--method sdo`.
2. **Read-back OK but no volts.** The command latch is set, so the fault is
   downstream: blown output fuse, module not fully seated, channel protection
   tripped, or a defective switching element. Compare a suspect module against a
   known-good one in the same slot.
3. **Measuring wrong.** Both rails float — measure positive against its own
   return, never against chassis.
4. **Wrong polarity.** If `on` produces nothing and `off` produces 12 V, you
   have an old-style crate: add `--invert` everywhere.
5. **Byte order.** The current framework model says RPDO1 byte 0 is port C, but
   an older generator in `fwElmbUser.ctl` labels byte 0 as port F. If exactly the
   wrong half of the branches respond, that is the tell — `--method sdo` writes
   each bit by explicit object dictionary entry and sidesteps the question
   entirely.

## 7. Making the crate come up powered

Once you are happy, set the power-up state so the branches are on after a mains
cycle without any software in the loop:

```bash
# example: all 16 branches on at power-up, on a production crate
./elmbpsu_can.py --iface can8 on all
# then persist the ELMB parameters (0x1010:01 <- "save")
./elmbpsu_can.py --iface can8 outmask 0xFF 0xFF --store
```

`doInitHigh` (0x2300) is the object that governs this; read it with `status`
before and after so you can see what changed.

## 8. Files here

| file | what it is |
|------|------------|
| `can_diag.py` | read-only pre-flight: OPC-UA servers, CAN interfaces, who owns what |
| `elmbpsu_can.py` | dependency-free SocketCAN control/diagnosis tool |
| `selftest.py` | offline verification of the above against a simulated ELMB |
| `config-elmbpsu.xml` | CanOpenOpcUa server configuration for one crate |
| `elmbpsu_opcua.py` | OPC-UA client — the fwElmbPSU replacement |
