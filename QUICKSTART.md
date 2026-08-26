# ELMB PSU — quick test

Assumes the control ELMB is wired to your CAN interface (and that both ends of
the control bus are terminated with 120 Ω — the crate does not terminate it for
you).

### 0. One-time setup

```bash
pip install asyncua
cd ~/can_psu_test
```

### 1. Find a CAN port that is actually free

This is a shared machine. Someone else's CanOpenOpcUa server or WinCC OA
project may already own a bus, and a second CANopen master on it will collide
with theirs. `can_diag.py` is read-only — it configures nothing and transmits
nothing — and tells you which OPC-UA servers are running, what CAN interfaces
exist, and which of them are in use.

```bash
./can_diag.py
```

Take an interface from the `FREE` line of its verdict. Everything below writes
it as `can13`; substitute what you were given.

### 2. Bring up the CAN interface

The ELMB PSU default is 125 kbit/s. Do this yourself rather than letting the
server do it, so the server does not need elevated privileges.

```bash
sudo ip link set can13 down
sudo ip link set can13 type can bitrate 125000
sudo ip link set can13 up
ip -details -statistics link show can13
```

Look for `state ERROR-ACTIVE` and a non-zero bitrate. `state BUS-OFF`, or
`error-warning`/`error-passive` counters climbing, means wiring or termination
trouble — fix that before going further.

### 3. Start the OPC-UA server

`config-elmbpsu.xml` is already set to `port="can13"` and `settings="125k"`.
Change `Bus/@port` if you picked a different interface, and `Node/@id` if your
node is not 63.

**The server must run with privileges.** CanModule opens a SocketCAN port by
taking the link down, setting the bitrate and bringing it back up, and it does
this unconditionally — `settings="DontConfigure"` and the
`force_dont_reconfigure` flag do *not* switch it off on the builds installed
here. Without privileges every call fails with
`RTNETLINK answers: Operation not permitted`, the device never opens, and you
get a stream of `Failed to send CAN frame: UNKNOWN_SEND_ERROR`.

Because the server sets the bitrate itself, step 2 is optional — it will bring
`can13` up for you.

Foreground, so you can watch the SDO traffic (Ctrl-C stops it; drive the client
from a second terminal):

```bash
sudo /opt/labTempMonitor/bin/CanOpenOpcUa \
    --config_file  $PWD/config-elmbpsu.xml \
    --opcua_backend_config $PWD/ServerConfig-elmbpsu.xml \
    --lSdo INF --lNodeMgmt INF --print_cobids_tables
```

Or detached, so it survives you closing the terminal:

```bash
sudo nohup /opt/labTempMonitor/bin/CanOpenOpcUa \
    --config_file  $PWD/config-elmbpsu.xml \
    --opcua_backend_config $PWD/ServerConfig-elmbpsu.xml \
    --lSdo INF --lNodeMgmt INF --print_cobids_tables \
    > server.log 2>&1 &
```

If you would rather not run it as root, grant just the one capability it needs
to a private copy:

```bash
mkdir -p ~/bin && cp /opt/labTempMonitor/bin/CanOpenOpcUa ~/bin/
sudo setcap cap_net_admin+ep ~/bin/CanOpenOpcUa
~/bin/CanOpenOpcUa --config_file $PWD/config-elmbpsu.xml ...
```

`--opcua_backend_config` is **not optional here**, despite what `--help` says.
Its default is `/opt/labTempMonitor/bin/ServerConfig.xml`, which declares port
**33815** — already bound by the lab temperature monitor that has been running
since July. `ServerConfig-elmbpsu.xml` is that same file with the port changed
to 48012, which `can_diag.py` confirmed is free.

Watch it come up, then leave it:

```bash
tail -f server.log        # Ctrl-C detaches; the server keeps running
```

It listens on `opc.tcp://<host>:48012`, anonymous, no security. Add
`--endpoint opc.tcp://HOST:48012` to every command below if it is not local.
Stop it with `kill <pid>` — plain SIGTERM shuts it down cleanly.

### 4. Debug the CAN bus (only if step 5 comes back empty)

Watch the traffic passively. You should see the server's SYNC and node-guard
requests going out, and replies from `0x7FF & (0x700 + 63)` = `73F` coming back.

```bash
candump -ta can13
```

If nothing at all comes back, stop the server and probe every node id directly —
this also catches a crate whose node id was changed from the default 63.

```bash
./elmbpsu_can.py --iface can13 scan
```

**On this crate the scan returned node 57 (0x39), not the factory default 63.**
That is why the server's first attempts showed `SW Version ?.?` — it was polling
a node id that does not exist. `config-elmbpsu.xml` now says `id="57"`. If you
ever move to a different crate, scan again before assuming.

Still empty? Retry at 250000 and 50000 in step 2 before suspecting the wiring.
Do not run `elmbpsu_can.py` commands other than `scan`/`dump` while the server
is running — two CANopen masters on one node will confuse each other.

### 5. Does the crate answer?

Reads the control ELMB's NMT state, serial number and digital-output config.
You want `NMT state: OPERATIONAL` and both `dioOutputMask` values at `0xFF` —
that means the crate is talking and its output pins are actually outputs.
All 16 branches should report `OFF`.

```bash
./elmbpsu_opcua.py status
```

Or without the server at all, straight over SocketCAN:

```bash
./elmbpsu_can.py --iface can13 --node 57 info
./elmbpsu_can.py --iface can13 --node 57 status
```

Note `--node 57`: the tool's built-in default is the factory 63.

### 6. Switch one branch on, then off

Branch 0 is slot 0, top connector. Each command re-reads the ELMB afterwards
and prints `read-back OK` only if the hardware really latched it. Measure the
Burndy between each positive pin and *its own* return while it is on.

```bash
./elmbpsu_opcua.py on 0
./elmbpsu_opcua.py off 0
```

### 7. Read the voltage and current

Turn branch 0 back on and read its four monitoring channels. Expect roughly
12 V on both the CAN and AD rails, and small currents (~20 mA CAN, ~25 mA AD)
with nothing plugged into the branch.

```bash
./elmbpsu_opcua.py on 0
./elmbpsu_opcua.py mon --branches 0
```

`mon` defaults to `--source tpdo`, the SYNC-driven scan. `--source sdo` (the
on-request 0x2404 reads) returns `n/a` on this crate — see HANDOFF.md §8b.

### Done

```bash
./elmbpsu_opcua.py off all
```

---

If a switch reports `READ-BACK MISMATCH`, or `mon` shows 0 V while the state
says ON, see the troubleshooting ladder in `README.md` §6.
