# ELMB PSU crate — bring-up and control without WinCC OA

Tools to test and operate a CERN ELMB PSU crate over CANopen, either straight
over SocketCAN or through the CERN CanOpenOpcUa server — replacing WinCC OA +
fwElmbPSU + fwElmb, none of which is installed here.

Everything was reverse-engineered from `ElmbPsuIntroduction.pdf`, `fwElmbPSU/`,
`fwElmb/` and `CanOpenOpcUa/`, then verified against a real crate on 2026-08-25.

| | |
|---|---|
| **Start here** | [docs/QUICKSTART.md](docs/QUICKSTART.md) — the operator procedure, step by step |
| **Protocol, mappings, server behaviour** | [docs/REFERENCE.md](docs/REFERENCE.md) |
| **This crate, this machine, what is open** | [docs/HANDOFF.md](docs/HANDOFF.md) |
| **Which parameters to change** | [config/README.md](config/README.md) |

## Layout

```
setup.sh                  restores the workspace: reference repos, venv, checks
config/                   the two CanOpenOpcUa XML files + what to change in them
lib/elmbpsu_can.py        SocketCAN control/diagnosis tool, zero dependencies
lib/elmbpsu_opcua.py      OPC-UA client — the fwElmbPSU replacement
tests/can_diag.py         read-only pre-flight: OPC-UA servers, CAN ports, who owns what
tests/selftest.py         offline verification against a simulated ELMB, 27 checks
docs/                     QUICKSTART, REFERENCE, HANDOFF
```

`CanOpenOpcUa/`, `fwElmb/` and `fwElmbPSU/` appear at the root after setup. They
are read-only reference material, gitignored, and nothing here imports them.

## 1. Setup

```bash
git clone https://github.com/jimmat92/can_psu_test.git
cd can_psu_test
./setup.sh                 # add --ssh if you have a CERN GitLab SSH key
source .venv/bin/activate
```

`setup.sh` clones the pinned reference repos, builds `.venv`, and verifies the
workspace. The venv is what makes paths stop mattering — it installs `asyncua`,
puts `lib/` on the import path, exports `CAN_PSU_TEST` and `CAN_PSU_CONFIG`, and
puts the tools on `PATH`:

| activated command | is |
|---|---|
| `can-diag` | `tests/can_diag.py` |
| `elmbpsu-can` | `lib/elmbpsu_can.py` |
| `elmbpsu-opcua` | `lib/elmbpsu_opcua.py` |
| `elmbpsu-selftest` | `tests/selftest.py` |

`tests/can_diag.py` and `lib/elmbpsu_can.py` are standard library only, so they
also run by path with no venv — `./tests/can_diag.py`, `./lib/elmbpsu_can.py`.
`elmbpsu_opcua.py` needs `asyncua`, and `tests/selftest.py` imports
`elmbpsu_can` by name, so both of those want the venv active.

Useful flags: `--check` verifies an existing workspace without building,
`--no-venv` skips the venv, `--dest ..` puts the reference repos beside the repo
instead of inside it, `--submodules` inits CanModuleMain/LogIt if you intend to
build the server.

## 2. Check the machine before you touch anything

`pcaticstest08` is a **shared** machine. Other people run WinCC OA projects,
CanOpenOpcUa servers and CAN traffic on it, and a bus that already carries a
CANopen master is not yours to take. Run this first — it is read-only, it
configures no link and transmits no CAN frame:

```bash
./tests/can_diag.py
```

Three sections:

1. **OPC-UA servers running here.** Every listening TCP port is greeted with an
   OPC-UA `HELLO` (OPC 10000-6 §7.1.2); `ACK` or `ERR` proves an OPC-UA server,
   anything else disproves it. Confirmed servers are then asked `GetEndpoints`
   for their application name, so you see *which* server it is —
   `CanOpen@host` / `urn:CERN:CanOpenOpcUa` is another CanOpenOpcUa instance and
   very likely already owns a CAN port.
2. **What CAN interfaces exist**, with link state, CAN state, bitrate, frame
   counters and a live frame rate, grouped by the physical USB adapter behind
   them.
3. **Which are used and which are free.** "Used" comes from the kernel's own
   receive lists (`/proc/net/can/rcvlist_*`) — an entry there means a process
   holds a socket bound to that device, and it covers every user on the box
   whether or not you can read their `/proc`. Cross-checked against the live
   frame rate and, where possible, attributed to a PID via `lsof`.

`--json` gives the same data machine-readably; `--no-probe` stops it connecting
to anything.

At the time of writing this host has four **SysTec Multiport CAN-to-USB** boxes
of two ports each, `can8`…`can15`. **`can9` runs a production lab temperature
monitor — never touch it.** The crate is on `can13`. Do not assume that is still
true; that is what the script is for.

Once you have a free port, bring it up at the ELMB PSU default bitrate:

```bash
sudo ip link set can13 down
sudo ip link set can13 type can bitrate 125000
sudo ip link set can13 up
ip -details link show can13
```

**The control bus has no built-in terminators** — fit 120 Ω at both ends
yourself. DE-9 pinout (CiA-303): pin 2 = CAN-L, 3 = CAN GND, 7 = CAN-H,
9 = optional V+.

## 3. Fastest path: talk to the ELMB directly

`lib/elmbpsu_can.py` speaks CANopen straight over SocketCAN using CPython's
built-in `AF_CAN` — nothing to build, nothing to install. Use it to answer "is
the crate alive and can I switch a branch?" before investing in the OPC-UA stack.

This crate is **node 57 (0x39)**, not the factory default 63 — confirmed by a bus
scan. Pass `--node 57`; the tool's default is 63.

```bash
# 1. Is anything on the bus? (probes node-guard on all 127 node ids)
elmbpsu-can --iface can13 scan

# 2. Who answered?
elmbpsu-can --iface can13 --node 57 info

# 3. Current configuration and branch states
elmbpsu-can --iface can13 --node 57 status

# 4. Switch slot 0 position A (branch 0) on, with automatic read-back verify
elmbpsu-can --iface can13 --node 57 on 0

# 5. Read what the crate thinks the rails are doing
elmbpsu-can --iface can13 --node 57 mon --branches 0

# 6. Everything on / everything off
elmbpsu-can --iface can13 --node 57 on all
elmbpsu-can --iface can13 --node 57 off all
```

**Never point this at a bus `can-diag` reported as IN USE.** Two CANopen
masters on one bus collide on SDO transfers, and you would be switching branches
out from under whoever else is on it.

Useful extras: `-v` traces every CAN frame, `dump` is a passive candump,
`nmt start|preop|reset` drives the NMT state machine, and `--method sdo` uses
bitwise SDO writes to 0x6220 instead of the RPDO (works in PRE-OPERATIONAL,
handy when you suspect the node never reached OPERATIONAL).

`elmbpsu-selftest` exercises all of the above against a simulated ELMB — run it
to confirm the tool itself is sane before blaming the hardware.

## 4. The mapping, in one table

Derivation, sources and the full object dictionary are in
[docs/REFERENCE.md](docs/REFERENCE.md) §2–4. The short version:

```
        slot 0   slot 1   slot 2   slot 3   slot 4   slot 5   slot 6   slot 7
pos A     0        2        4        6        8       10       12       14
pos B     1        3        5        7        9       11       13       15

branch = 2*slot + (0 for position A/top, 1 for position B/bottom)
```

> **Bit N of the 16-bit DO word is branch N**, for all 16 branches.
> Branches 0–7 are port C, 8–15 are port A, and the word is `(portA << 8) | portC`.

Production crates are **1 = ON**; pre-2.0.0 crates are inverted and need
`--invert`. **This crate is a production one — do not use `--invert`.**

Monitoring, for branch *b* with `ch = 4*b − 2*(b mod 2)`: CAN voltage at `ch`,
AD voltage at `ch+1`, CAN current at `ch+4`, AD current at `ch+5`. Raw values are
signed microvolts:

```
voltage [V] = raw/1e6 * 100.0                    (100:1 divider on the module)
current [A] = (raw/1e6 - 2.5) * 5.0 / 0.625      (2.5 V-centred sensor, 8 A/V)
```

Nominal per branch: 12.0 V on both rails, 20 mA CAN and 25 mA AD unloaded.
What each *abnormal* reading means — absent module, floating sensor, switched-off
branch — is tabulated in [docs/REFERENCE.md](docs/REFERENCE.md) §4.

## 5. The OPC-UA route

### Run the server

Two builds are installed on `pcaticstest08`. Use `/opt/labTempMonitor/`, the
tagged `v0.10.1` release the lab actually runs; `/opt/CanOpenOpcUa/bin/` holds a
newer but `-dirty` pipeline build.

```bash
sudo /opt/labTempMonitor/bin/CanOpenOpcUa \
    --config_file          $CAN_PSU_CONFIG/config-elmbpsu.xml \
    --opcua_backend_config $CAN_PSU_CONFIG/ServerConfig-elmbpsu.xml
```

Two things bite here, both explained in full in
[docs/REFERENCE.md](docs/REFERENCE.md) §6:

- **It needs privileges, and `DontConfigure` will not save you.** CanModule takes
  the link down, sets the bitrate and brings it up unconditionally. Without
  privileges every call returns `Operation not permitted`; with `DontConfigure`
  it still enters that path with bitrate 0, fails `Invalid argument`, and
  **leaves your port DOWN**. `--force_dont_reconfigure` logs that it is forcing
  the mode and then does nothing. Run it under `sudo`, or give a private copy
  just the one capability:

  ```bash
  mkdir -p ~/bin && cp /opt/labTempMonitor/bin/CanOpenOpcUa ~/bin/
  sudo setcap cap_net_admin+ep ~/bin/CanOpenOpcUa
  ```

- **`--opcua_backend_config` is mandatory.** It defaults to the
  `ServerConfig.xml` next to the *binary*, which asks for port 33815 — held by
  the lab temperature monitor since July. Ours asks for 48012.

The endpoint is then `opc.tcp://<host>:48012`, anonymous, security `None`.
`kill <pid>` (plain SIGTERM) shuts it down cleanly, as does Ctrl-C in the
foreground. To background it: `nohup ... > server.log 2>&1 &`, or `tmux new -s
psu` if you want to watch it and keep a terminal.

### Run the client

```bash
elmbpsu-opcua --endpoint opc.tcp://localhost:48012 status
elmbpsu-opcua --endpoint opc.tcp://localhost:48012 on 0
elmbpsu-opcua --endpoint opc.tcp://localhost:48012 off all
elmbpsu-opcua --endpoint opc.tcp://localhost:48012 mon --branches 0-3
elmbpsu-opcua --endpoint opc.tcp://localhost:48012 browse --depth 2
```

It resolves the `OPCUASERVER` namespace index at runtime, writes correctly-typed
Variants (quasar declares these nodes as `BaseDataType`, so an untyped write is
rejected), and verifies every switch by reading 0x6200 back.

`mon` defaults to `--source tpdo`, the SYNC-driven scan. `--source sdo` (the
on-request 0x2404 reads) returns `n/a` on this crate.

## 6. If a branch still shows 0 V after a verified switch-on

Work down this list.

0. **Read-back came back `0x0000` when you only asked for one branch.** This is
   the RPDO cache trap, not a hardware fault: `RPDO1.branchNN` is a quasar
   `RpdoCachedVariable`, and writing it read-modify-writes the **server's** shadow
   cache, which starts at all zeros and knows nothing about the state the crate
   powered up in. `elmbpsu_opcua.py` now writes the full 16-bit word for
   `--method rpdo` so this cannot happen. If you hit it with an older copy or
   another client, cache and crate are in sync afterwards — re-issue the command,
   and `on all` to restore. Details in [docs/REFERENCE.md](docs/REFERENCE.md) §6.

1. **Read-back mismatch reported by the tool.** The ELMB did not latch the write.
   Check `dioOutputMaskC`/`dioOutputMaskA` are `0xFF` (`status`); if not,
   `outmask 0xFF 0xFF --store` sets them and persists to EEPROM. Confirm the node
   was OPERATIONAL, and try `--method sdo`.
2. **Read-back OK but no volts.** The command latch is set, so the fault is
   downstream: blown output fuse, module not fully seated, channel protection
   tripped, or a defective switching element. Compare a suspect module against a
   known-good one in the same slot. Check the current channel too — a floating
   current sense with a healthy voltage points at seating, not the output stage.
3. **Measuring wrong.** Both rails float — measure positive against **its own
   return**, never against chassis. This turned out to be the entire original
   "every output reads 0 V" report.
4. **Wrong polarity.** If `on` produces nothing and `off` produces 12 V, you have
   an old-style crate: add `--invert` everywhere. Not this one.
5. **Byte order.** The current framework model says RPDO1 byte 0 is port C, but a
   legacy generator in `fwElmbUser.ctl` labels byte 0 as port F. If exactly the
   wrong half of the branches respond, that is the tell — `--method sdo` writes
   each bit by explicit object-dictionary entry and sidesteps it.

Do not have the Burndy pinout? You do not need it — use the crate as its own
signal generator, [docs/REFERENCE.md](docs/REFERENCE.md) §5.

## 7. Making the crate come up powered

Set the power-up state so the branches are on after a mains cycle with no
software in the loop:

```bash
elmbpsu-can --iface can13 --node 57 on all
elmbpsu-can --iface can13 --node 57 outmask 0xFF 0xFF --store   # 0x1010:01 <- "save"
```

`doInitHigh` (0x2300) is the object that governs this; read it with `status`
before and after so you can see what changed. On this crate it is already `0x01`.
