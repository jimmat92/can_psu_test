# ELMB PSU crate — bring-up and control without WinCC OA

Test and operate a CERN ELMB PSU crate over CANopen, either straight over
SocketCAN or through the CERN CanOpenOpcUa server — replacing WinCC OA +
fwElmbPSU + fwElmb, none of which is installed here.

Reverse-engineered from `ElmbPsuIntroduction.pdf`, `fwElmbPSU/`, `fwElmb/` and
`CanOpenOpcUa/`, then verified against a real crate on 2026-08-25.

| | |
|---|---|
| **Start here** | [docs/QUICKSTART.md](docs/QUICKSTART.md) — the operator procedure |
| **Protocol, mappings, server behaviour** | [docs/REFERENCE.md](docs/REFERENCE.md) |
| **This crate, this machine, what is open** | [docs/HANDOFF.md](docs/HANDOFF.md) |
| **Which parameters to change** | [config/README.md](config/README.md) |

## Layout

```
setup.sh                  restores the workspace: reference repos, venv, checks
config/                   the two CanOpenOpcUa XML files + what to change in them
lib/elmbpsu_can.py        SocketCAN control/diagnosis tool, zero dependencies
lib/elmbpsu_opcua.py      OPC-UA client — the fwElmbPSU replacement
lib/elmbpsu_server.py     start/stop the CanOpenOpcUa server itself
tests/can_diag.py         read-only pre-flight: OPC-UA servers, CAN ports, who owns what
tests/selftest.py         offline verification against a simulated ELMB, 27 checks
tests/smoke_test.py       start server, scan bus, ping, stop server
tests/crate_scan.py       full sweep: occupancy, switching, sensor health
docs/                     QUICKSTART, REFERENCE, HANDOFF
```

`CanOpenOpcUa/`, `fwElmb/` and `fwElmbPSU/` appear at the root after setup —
read-only reference, gitignored, never imported.

## 1. Setup

```bash
git clone https://github.com/jimmat92/can_psu_test.git
cd can_psu_test
./setup.sh                 # add --ssh if you have a CERN GitLab SSH key
source .venv/bin/activate
```

`setup.sh` clones the pinned reference repos, builds `.venv` (asyncua, `lib/` on
the import path, `CAN_PSU_TEST`/`CAN_PSU_CONFIG` exported), and verifies the
workspace. Activating the venv puts the tools on `PATH`:

| command | is | command | is |
|---|---|---|---|
| `can-diag` | `tests/can_diag.py` | `elmbpsu-server` | `lib/elmbpsu_server.py` |
| `elmbpsu-can` | `lib/elmbpsu_can.py` | `elmbpsu-selftest` | `tests/selftest.py` |
| `elmbpsu-opcua` | `lib/elmbpsu_opcua.py` | `elmbpsu-smoketest` | `tests/smoke_test.py` |
| | | `elmbpsu-cratescan` | `tests/crate_scan.py` |

`can_diag.py` and `elmbpsu_can.py` are stdlib-only and also run by path with no
venv. Everything else needs `asyncua` or imports `lib/` by name.

Flags: `--check` verifies without building, `--no-venv` skips the venv,
`--dest ..` puts the reference repos beside the repo, `--submodules` inits
CanModuleMain/LogIt if you intend to build the server.

## 2. Check the machine before you touch anything

`pcaticstest08` is **shared**, and a bus that already carries a CANopen master
is not yours to take. Run this first — read-only, configures no link, transmits
no frame:

```bash
./tests/can_diag.py          # --json for machine-readable, --no-probe to skip connecting
```

It reports OPC-UA servers running here (each listening port gets an OPC-UA
`HELLO` per OPC 10000-6 §7.1.2 — `ACK` or `ERR` proves a server — then
`GetEndpoints` for its application name, so you see *which* server it is), every
CAN interface with link/CAN state, bitrate and live frame rate, and which are
free. "Used" comes from the kernel's own receive lists
(`/proc/net/can/rcvlist_*`), so it covers every user on the box whether or not
you can read their `/proc`.

This host has four **SysTec Multiport CAN-to-USB** boxes, `can8`…`can15`.
**`can9` runs a production lab temperature monitor — never touch it.** The crate
is on `can13`. Do not assume that is still true; that is what the script is for.

Bring a free port up at the ELMB PSU default bitrate:

```bash
sudo ip link set can13 down
sudo ip link set can13 type can bitrate 125000
sudo ip link set can13 up
```

**The control bus has no built-in terminators** — fit 120 Ω at both ends
yourself. DE-9 (CiA-303): pin 2 = CAN-L, 3 = CAN GND, 7 = CAN-H, 9 = optional V+.

## 3. Fastest path: talk to the ELMB directly

`lib/elmbpsu_can.py` speaks CANopen straight over SocketCAN using CPython's
built-in `AF_CAN` — nothing to build or install. Use it to answer "is the crate
alive and can I switch a branch?" before investing in the OPC-UA stack.

This crate is **node 57 (0x39)**, not the factory default 63 — confirmed by a
bus scan. Pass `--node 57`; the tool's default is 63.

```bash
elmbpsu-can --iface can13 scan                      # probe node-guard on all 127 ids
elmbpsu-can --iface can13 --node 57 info            # who answered
elmbpsu-can --iface can13 --node 57 status          # config and branch states
elmbpsu-can --iface can13 --node 57 on 0            # slot 0 pos A, with read-back verify
elmbpsu-can --iface can13 --node 57 mon --branches 0
elmbpsu-can --iface can13 --node 57 on all
elmbpsu-can --iface can13 --node 57 off all
```

**Never point this at a bus `can-diag` reported as IN USE** — two CANopen
masters collide on SDO transfers, and you would be switching branches out from
under whoever else is on it.

Extras: `-v` traces every CAN frame, `dump` is a passive candump,
`nmt start|preop|reset` drives the NMT state machine, `--method sdo` uses
bitwise SDO writes to 0x6220 instead of the RPDO (works in PRE-OPERATIONAL,
handy when you suspect the node never reached OPERATIONAL).

`elmbpsu-selftest` exercises all of the above against a simulated ELMB — run it
to confirm the tool is sane before blaming the hardware.

## 4. The mapping, in one table

Derivation and sources: [docs/REFERENCE.md](docs/REFERENCE.md) §2–4.

```
        slot 0   slot 1   slot 2   slot 3   slot 4   slot 5   slot 6   slot 7
pos A     0        2        4        6        8       10       12       14
pos B     1        3        5        7        9       11       13       15

branch = 2*slot + (0 for position A/top, 1 for position B/bottom)
```

> **Bit N of the 16-bit DO word is branch N**, for all 16 branches.
> Branches 0–7 are port C, 8–15 are port A, word = `(portA << 8) | portC`.

Production crates are **1 = ON**; pre-2.0.0 crates are inverted and need
`--invert`. **This crate is a production one — do not use `--invert`.**

Monitoring, for branch *b* with `ch = 4*b − 2*(b mod 2)`: CAN voltage at `ch`,
AD voltage at `ch+1`, CAN current at `ch+4`, AD current at `ch+5`. Raw values
are signed microvolts:

```
voltage [V] = raw/1e6 * 100.0                    (100:1 divider on the module)
current [A] = (raw/1e6 - 2.5) * 5.0 / 0.625      (2.5 V-centred sensor, 8 A/V)
```

Nominal per branch: 12.0 V on both rails, 20 mA CAN and 25 mA AD unloaded. What
each *abnormal* reading means is tabulated in
[docs/REFERENCE.md](docs/REFERENCE.md) §4.

## 5. The OPC-UA route

```bash
sudo /opt/labTempMonitor/bin/CanOpenOpcUa \
    --config_file          $CAN_PSU_CONFIG/config-elmbpsu.xml \
    --opcua_backend_config $CAN_PSU_CONFIG/ServerConfig-elmbpsu.xml
```

Use `/opt/labTempMonitor/`, the tagged `v0.10.1` the lab actually runs;
`/opt/CanOpenOpcUa/bin/` is a newer `-dirty` pipeline build. Both flags are
mandatory and **the server needs privileges** — full explanation of both traps
in [docs/REFERENCE.md](docs/REFERENCE.md) §6. Endpoint is
`opc.tcp://<host>:48012`, anonymous, security `None`; `kill <pid>` stops it
cleanly.

```bash
elmbpsu-opcua --endpoint opc.tcp://localhost:48012 status
elmbpsu-opcua on 0
elmbpsu-opcua off all
elmbpsu-opcua mon --branches 0-3
elmbpsu-opcua browse --depth 2
```

It resolves the `OPCUASERVER` namespace index at runtime, writes correctly-typed
Variants (quasar declares these nodes `BaseDataType`, so untyped writes are
rejected), and verifies every switch by reading 0x6200 back. `mon` defaults to
`--source tpdo`; `--source sdo` returns `n/a` on this crate.

Two test scripts drive the whole lifecycle themselves:

```bash
elmbpsu-smoketest                          # start server, scan bus, ping, stop
elmbpsu-cratescan -n 10 --json scan.json   # occupancy, switching, sensor health
```

`elmbpsu-cratescan` **power-cycles every branch**; `--skip-switch-test` makes it
read-only. See [docs/QUICKSTART.md](docs/QUICKSTART.md).

## 6. If a branch still shows 0 V after a verified switch-on

0. **Read-back `0x0000` when you only asked for one branch** — the RPDO cache
   trap, not a hardware fault ([REFERENCE.md](docs/REFERENCE.md) §6). Cache and
   crate are in sync afterwards: re-issue, then `on all` to restore.
1. **Read-back mismatch.** The ELMB did not latch the write. Check
   `dioOutputMaskC`/`dioOutputMaskA` are `0xFF` (`status`); if not,
   `outmask 0xFF 0xFF --store`. Confirm the node was OPERATIONAL, try
   `--method sdo`.
2. **Read-back OK but no volts.** Fault is downstream: blown fuse, module not
   fully seated, protection tripped, defective switching element. Compare
   against a known-good module in the same slot. A floating current sense with a
   healthy voltage points at seating, not the output stage.
3. **Measuring wrong.** Both rails float — measure positive against **its own
   return**, never chassis. This was the entire original "every output reads
   0 V" report.
4. **Wrong polarity.** `on` gives nothing and `off` gives 12 V → old-style
   crate, add `--invert`. Not this one.
5. **Byte order.** A legacy generator in `fwElmbUser.ctl` labels RPDO1 byte 0 as
   port F, not port C. If exactly the wrong half of the branches respond, that
   is the tell — `--method sdo` sidesteps it.

No Burndy pinout? You do not need it — use the crate as its own signal
generator, [docs/REFERENCE.md](docs/REFERENCE.md) §5.

## 7. Making the crate come up powered

```bash
elmbpsu-can --iface can13 --node 57 on all
elmbpsu-can --iface can13 --node 57 outmask 0xFF 0xFF --store   # 0x1010:01 <- "save"
```

`doInitHigh` (0x2300) governs the power-up state; read it with `status` before
and after. On this crate it is already `0x01`.
