# ELMB PSU — quick test

Assumes the control ELMB is wired to your CAN interface, and that both ends of
the control bus are terminated with 120 Ω — the crate does not terminate it for
you.

Background on anything below: [REFERENCE.md](REFERENCE.md). Current state of
this particular crate: [HANDOFF.md](HANDOFF.md).

### 0. One-time setup

```bash
cd ~/can_psu_test
./setup.sh
source .venv/bin/activate
```

That installs `asyncua`, exports `CAN_PSU_CONFIG`, and puts `can-diag`,
`elmbpsu-can` and `elmbpsu-opcua` on your `PATH`. Re-activate the venv in every
new terminal.

### 1. Find a CAN port that is actually free

This is a shared machine. Someone else's CanOpenOpcUa server or WinCC OA project
may already own a bus, and a second CANopen master on it will collide with
theirs. `tests/can_diag.py` is read-only — it configures nothing and transmits nothing.

```bash
./tests/can_diag.py
```

Take an interface from the `FREE` line of its verdict. Everything below writes it
as `can13`; substitute what you were given. **`can9` is the lab temperature
monitor — never take it.**

### 2. Bring up the CAN interface

The ELMB PSU default is 125 kbit/s.

```bash
sudo ip link set can13 down
sudo ip link set can13 type can bitrate 125000
sudo ip link set can13 up
ip -details -statistics link show can13
```

Look for `state ERROR-ACTIVE` and a non-zero bitrate. `state BUS-OFF`, or
`error-warning`/`error-passive` counters climbing, means wiring or termination
trouble — fix that before going further.

This step is optional if you are starting the server: it sets the bitrate itself
(step 3). It is required for `elmbpsu-can`, which never touches link config.

Or as one call, printing the same state back nicely afterwards:

```bash
elmbpsu-can --iface can13 linkup
```

```python
from elmbpsu_can import bring_up_can
info = bring_up_can("can13")            # or bring_up_can(13)
```

### 3. Start the OPC-UA server

`config/config-elmbpsu.xml` is already set to `port="can13"`, `settings="125k"`
and `id="57"`. Change `Bus/@port` if you picked a different interface, and
`Node/@id` if your scan (step 4) reports a different node —
[../config/README.md](../config/README.md) lists everything worth changing.

**The server must run with privileges.** CanModule opens a SocketCAN port by
taking the link down, setting the bitrate and bringing it back up, and it does
this unconditionally — `settings="DontConfigure"` and `--force_dont_reconfigure`
do *not* switch it off on the builds installed here. Without privileges every
call fails `RTNETLINK answers: Operation not permitted`, the device never opens,
and you get a stream of `Failed to send CAN frame: UNKNOWN_SEND_ERROR`. The
OPC-UA endpoint still opens, which makes it easy to miss.

Foreground, so you can watch the SDO traffic (Ctrl-C stops it; drive the client
from a second terminal):

```bash
sudo /opt/labTempMonitor/bin/CanOpenOpcUa \
    --config_file          $CAN_PSU_CONFIG/config-elmbpsu.xml \
    --opcua_backend_config $CAN_PSU_CONFIG/ServerConfig-elmbpsu.xml \
    --lSdo INF --lNodeMgmt INF --print_cobids_tables
```

Or detached, so it survives you closing the terminal:

```bash
sudo nohup /opt/labTempMonitor/bin/CanOpenOpcUa \
    --config_file          $CAN_PSU_CONFIG/config-elmbpsu.xml \
    --opcua_backend_config $CAN_PSU_CONFIG/ServerConfig-elmbpsu.xml \
    --lSdo INF --lNodeMgmt INF --print_cobids_tables \
    > server.log 2>&1 &
```

If you would rather not run it as root, grant just the one capability it needs to
a private copy:

```bash
mkdir -p ~/bin && cp /opt/labTempMonitor/bin/CanOpenOpcUa ~/bin/
sudo setcap cap_net_admin+ep ~/bin/CanOpenOpcUa
~/bin/CanOpenOpcUa --config_file $CAN_PSU_CONFIG/config-elmbpsu.xml ...
```

`--opcua_backend_config` is **not optional here**, despite what `--help` says.
Its default is `/opt/labTempMonitor/bin/ServerConfig.xml`, which declares port
**33815** — already bound by the lab temperature monitor that has been running
since July. `ServerConfig-elmbpsu.xml` is that same file with the port changed to
48012.

Watch it come up, then leave it:

```bash
tail -f server.log        # Ctrl-C detaches; the server keeps running
```

It listens on `opc.tcp://<host>:48012`, anonymous, no security. Add
`--endpoint opc.tcp://HOST:48012` to every command below if it is not local.
Stop it with `kill <pid>` — plain SIGTERM shuts it down cleanly.

Sanity check while it starts: the node table it prints should show a real
`SW Version`. `?.?` means it never exchanged a frame with the crate — wrong node
id, or the privilege failure above.

**Or let Python own the lifecycle** instead of a second terminal:
`lib/elmbpsu_server.py` runs the same command under `sudo`, finds its real pid
with `pgrep -f`, and can stop it with a clean SIGTERM.

```bash
elmbpsu-server start --wait-port     # blocks until the OPC-UA port is listening
elmbpsu-server status
elmbpsu-server stop
```

Or from a test script, as a context manager:

```python
from elmbpsu_server import OpcUaServer
with OpcUaServer() as server:            # start() on entry, stop() on exit
    server.wait_for_port()
    ...                                   # drive elmbpsu_opcua.PsuCrate here
```

### 4. Debug the CAN bus (only if step 5 comes back empty)

Watch the traffic passively. You should see the server's SYNC and node-guard
requests going out, and replies from `0x700 + node` coming back — `739` for
node 57.

```bash
candump -ta can13
```

If nothing comes back, stop the server and probe every node id directly:

```bash
elmbpsu-can --iface can13 scan
```

**On this crate the scan returned node 57 (0x39), not the factory default 63.**
That is why the server's first attempts showed `SW Version ?.?`.
`config/config-elmbpsu.xml` now says `id="57"`. If you move to a different crate,
scan again before assuming.

Still empty? Retry at 250000 and 50000 in step 2 before suspecting the wiring.
**Do not run `elmbpsu-can` commands other than `scan` and `dump` while the server
is running** — two CANopen masters on one node will confuse each other.

### 5. Does the crate answer?

Reads the control ELMB's NMT state, serial number and digital-output config.
You want `NMT state: OPERATIONAL` and both `dioOutputMask` values at `0xFF` —
that means the crate is talking and its output pins really are outputs.

```bash
elmbpsu-opcua status
```

Cheaper sanity check first, if you just want to know the server is reachable
and the config's node-id naming resolves — one read (`stateAsText`), through
the address space `config-elmbpsu.xml` built, not raw CANopen:

```bash
elmbpsu-opcua ping
```

Or without the server at all, straight over SocketCAN:

```bash
elmbpsu-can --iface can13 --node 57 info
elmbpsu-can --iface can13 --node 57 status
```

Note `--node 57`: the tool's built-in default is the factory 63.

Branch states depend on `doInitHigh`, which on this crate is `0x01`, so a
freshly power-cycled crate comes up with **all sixteen branches ON**. At the time
of writing only branch 0 is on — `elmbpsu-opcua on all` restores the rest.

### 6. Switch one branch on, then off

Branch 0 is slot 0, top connector. Each command re-reads the ELMB afterwards and
prints `read-back OK` only if the hardware really latched it.

```bash
elmbpsu-opcua on 0
elmbpsu-opcua off 0
```

Measure the Burndy between each positive pin and **its own return**, never
against chassis — both rails float. Do not have the pinout? You do not need it;
switching a branch off and watching which pin pair drops identifies it
([REFERENCE.md](REFERENCE.md) §5).

### 7. Read the voltage and current

```bash
elmbpsu-opcua on 0
elmbpsu-opcua mon --branches 0
```

Expect roughly 12 V on both the CAN and AD rails and small currents (~20 mA CAN,
~25 mA AD) with nothing plugged in. A branch reading ~0.01 V with a current of
about −19.8 A is an **empty slot**, not a fault — see
[REFERENCE.md](REFERENCE.md) §4 for what each pattern means.

`mon` defaults to `--source tpdo`, the SYNC-driven scan. `--source sdo` (the
on-request 0x2404 reads) returns `n/a` on this crate. TPDO3 refreshes once per
SYNC, i.e. every `syncIntervalMs` — 10 s by default, so drop it to ~1000 in the
config if you are watching something change.

### Done

```bash
elmbpsu-opcua off all
```

### One-shot bring-up check

`tests/smoke_test.py` does steps 3–5 for you: starts the server, bus-scans and
prints the node id(s) it finds (terminating if the bus is empty), pings the
crate through the OPC-UA server (terminating if that fails), then always stops
the server on the way out -- success, a failed check, Ctrl-C, or any error.

```bash
elmbpsu-smoketest
```

---

If a switch reports `READ-BACK MISMATCH`, or `mon` shows 0 V while the state says
ON, work down the ladder in [../README.md](../README.md) §6. The first entry —
read-back `0x0000` when you asked for one branch — is the RPDO cache trap and is
not a hardware fault.
