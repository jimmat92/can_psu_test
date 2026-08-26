# ELMB PSU — quick test

Assumes the control ELMB is wired to your CAN interface and both ends of the
control bus are terminated with 120 Ω — the crate does not terminate it for you.

Background: [REFERENCE.md](REFERENCE.md). State of this crate:
[HANDOFF.md](HANDOFF.md).

### 0. One-time setup

```bash
cd ~/can_psu_test
./setup.sh
source .venv/bin/activate      # re-activate in every new terminal
```

### 1. Find a CAN port that is actually free

Shared machine: a second CANopen master on someone else's bus will collide with
theirs. `tests/can_diag.py` is read-only — configures nothing, transmits nothing.

```bash
./tests/can_diag.py
```

Take an interface from the `FREE` line of its verdict. Everything below writes
it as `can13`. **`can9` is the lab temperature monitor — never take it.**

### 2. Bring up the CAN interface

ELMB PSU default is 125 kbit/s.

```bash
elmbpsu-can --iface can13 linkup        # or: ip link set down / type can bitrate 125000 / up
ip -details -statistics link show can13
```

Want `state ERROR-ACTIVE` and a non-zero bitrate. `BUS-OFF`, or
`error-warning`/`error-passive` counters climbing, means wiring or termination
trouble — fix that first.

Optional if you are starting the server (step 3 sets the bitrate itself).
Required for `elmbpsu-can`, which never touches link config. From Python:
`bring_up_can("can13")` in `elmbpsu_can`.

### 3. Start the OPC-UA server

`config/config-elmbpsu.xml` is already `port="can13"`, `settings="125k"`,
`id="57"`. Change `Bus/@port` for a different interface and `Node/@id` if your
scan (step 4) reports a different node — [../config/README.md](../config/README.md)
lists everything worth changing.

**It must run with privileges**, and `DontConfigure` is not a way out — without
them the CAN device never opens, every frame fails `UNKNOWN_SEND_ERROR`, and
**the OPC-UA endpoint still opens, which makes it easy to miss**
([REFERENCE.md](REFERENCE.md) §6). `--opcua_backend_config` is likewise **not
optional**, despite `--help`: its default declares port 33815, held by the lab
temperature monitor since July.

Foreground, watching the SDO traffic (Ctrl-C stops it; drive the client from a
second terminal):

```bash
sudo /opt/labTempMonitor/bin/CanOpenOpcUa \
    --config_file          $CAN_PSU_CONFIG/config-elmbpsu.xml \
    --opcua_backend_config $CAN_PSU_CONFIG/ServerConfig-elmbpsu.xml \
    --lSdo INF --lNodeMgmt INF --print_cobids_tables
```

Detached: prefix `nohup`, append `> server.log 2>&1 &`, then `tail -f
server.log`. Not as root: `setcap cap_net_admin+ep` on a private copy of the
binary.

It listens on `opc.tcp://<host>:48012`, anonymous, no security. Add
`--endpoint opc.tcp://HOST:48012` to the commands below if it is not local.
`kill <pid>` stops it cleanly.

Sanity check while it starts: the node table should show a real `SW Version`.
`?.?` means it never exchanged a frame with the crate — wrong node id, or the
privilege failure above.

**Or let Python own the lifecycle:**

```bash
elmbpsu-server start --wait-ready    # blocks until the endpoint opens
elmbpsu-server status
elmbpsu-server stop
```

```python
from elmbpsu_server import OpcUaServer
with OpcUaServer() as server:            # start() on entry, stop() on exit
    server.wait_ready()
    ...                                   # drive elmbpsu_opcua.PsuCrate here
```

`--wait-ready` watches `server.log` for `Opened endpoint` rather than
TCP-probing: the endpoint URL substitutes `[NodeName]` with the **hostname**,
which need not resolve to `127.0.0.1`, so probing loopback can spin for the
whole timeout while the server is up and healthy.

The endpoint opening is **not** the crate answering — reads return
`BadWaitingForInitialData` for up to a node-guard or SYNC period afterwards
([REFERENCE.md](REFERENCE.md) §6). `PsuCrate.wait_ready()` covers that.

### 4. Debug the CAN bus (only if step 5 comes back empty)

```bash
candump -ta can13
```

You should see the server's SYNC and node-guard requests going out, and replies
from `0x700 + node` — `739` for node 57. If nothing comes back, stop the server
and probe every node id directly:

```bash
elmbpsu-can --iface can13 scan
```

**On this crate the scan returned node 57 (0x39), not the factory default 63** —
which is why the server's first attempts showed `SW Version ?.?`. Scan again on
a different crate. Still empty? Retry at 250000 and 50000 in step 2 before
suspecting the wiring.

**Do not run `elmbpsu-can` commands other than `scan` and `dump` while the
server is running** — two CANopen masters on one node will confuse each other.

### 5. Does the crate answer?

Want `NMT state: OPERATIONAL` and both `dioOutputMask` values at `0xFF` — the
crate is talking and its output pins really are outputs.

```bash
elmbpsu-opcua status
elmbpsu-opcua ping                      # cheaper: one stateAsText read
```

Or without the server at all (note `--node 57`; the tool's default is the
factory 63):

```bash
elmbpsu-can --iface can13 --node 57 info
elmbpsu-can --iface can13 --node 57 status
```

Branch states depend on `doInitHigh`, `0x01` here, so a freshly power-cycled
crate comes up with **all sixteen branches ON**. At the time of writing only
branch 0 is on — `elmbpsu-opcua on all` restores the rest.

### 6. Switch one branch on, then off

Branch 0 is slot 0, top connector. Each command re-reads the ELMB afterwards and
prints `read-back OK` only if the hardware really latched it.

```bash
elmbpsu-opcua on 0
elmbpsu-opcua off 0
```

Measure the Burndy between each positive pin and **its own return**, never
against chassis — both rails float. No pinout? You do not need it; switching a
branch off and watching which pin pair drops identifies it
([REFERENCE.md](REFERENCE.md) §5).

### 7. Read the voltage and current

```bash
elmbpsu-opcua on 0
elmbpsu-opcua mon --branches 0
```

Expect ~12 V on both rails and small currents (~20 mA CAN, ~25 mA AD) unloaded.
~0.01 V with about −19.8 A is an **empty slot**, not a fault —
[REFERENCE.md](REFERENCE.md) §4 has the full pattern table.

`mon` defaults to `--source tpdo`; `--source sdo` returns `n/a` on this crate.
TPDO3 refreshes once per SYNC, i.e. every `syncIntervalMs` — 10 s by default, so
drop it to ~1000 in the config if you are watching something change.

### Done

```bash
elmbpsu-opcua off all
```

---

### One-shot bring-up check

`tests/smoke_test.py` does steps 3–5: starts the server, bus-scans and prints
the node id(s) found, pings the crate through the server, then always stops the
server on the way out — success, a failed check, Ctrl-C, or any error. It
terminates on an empty bus or a failed ping.

```bash
elmbpsu-smoketest
```

### Full crate scan

Once the smoke test passes, `tests/crate_scan.py` characterises the crate: all
64 analog inputs as found, then everything off, then everything on, then N
repeats for a mean and variance per channel.

```bash
elmbpsu-cratescan                       # 5 repeats, starts and stops the server
elmbpsu-cratescan -n 20 --json scan.json
elmbpsu-cratescan --skip-switch-test    # read-only: switches nothing
elmbpsu-cratescan --use-running-server  # attach to a server already up
```

It reports **per slot, not per branch** — a module spans branches `2*slot` and
`2*slot+1`, so a fault on either condemns that one module:

- which slots hold a module, and which are empty;
- whether on/off works, split into the command path (DO read-back) and the
  physical response (the rail actually going down and back up);
- whether each sensor reads something plausible *and* reads it steadily. Both
  halves matter: a sense line that nothing drives sits rock steady at a few
  hundred mV, so a stdev test alone would pass it.

Exit status is 0 only if nothing was found. It ends with a `SUSPECT MODULES`
line: the slots to pull.

Presence is decided from the **current** inputs. A voltage input reads ~0
whether the branch is off or the slot is empty, but a current input is held at
2.5 V by a reference the module powers itself — so it stays at 2.5 V with the
branch off and floats only when there is no module. That is why the all-off step
is a measurement and not just a switch test ([REFERENCE.md](REFERENCE.md) §4).

**It power-cycles every branch.** Use `--skip-switch-test` if anything is
plugged in that should not be. With `syncIntervalMs` at 10000 each of the ~8
scans costs 10 s; drop it to 1000 for roughly a tenfold speedup.

---

If a switch reports `READ-BACK MISMATCH`, or `mon` shows 0 V while the state
says ON, work down the ladder in [../README.md](../README.md) §6. The first
entry — read-back `0x0000` when you asked for one branch — is the RPDO cache
trap, not a hardware fault.
