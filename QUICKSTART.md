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
it as `can8`; substitute what you were given.

### 2. Bring up the CAN interface

The ELMB PSU default is 125 kbit/s. Do this yourself rather than letting the
server do it, so the server does not need elevated privileges.

```bash
sudo ip link set can8 down
sudo ip link set can8 type can bitrate 125000
sudo ip link set can8 up
ip -details -statistics link show can8
```

Look for `state ERROR-ACTIVE` and a non-zero bitrate. `state BUS-OFF`, or
`error-warning`/`error-passive` counters climbing, means wiring or termination
trouble — fix that before going further.

### 3. Start the OPC-UA server

Edit `config-elmbpsu.xml` first: set `Bus/@port` to the interface you picked in
step 1, and `Node/@id` if your node is not 63. Since step 2 already set the
bitrate, change `settings="125k"` to
`settings="DontConfigure"` in that file. Leave this running in its own terminal.

```bash
/opt/CanOpenOpcUa/bin/CanOpenOpcUa --config_file config-elmbpsu.xml \
    --lSdo INF --lNodeMgmt INF --print_cobids_tables
```

It listens on `opc.tcp://<host>:48012`, anonymous, no security — `can_diag.py`
in step 1 already told you whether that port was taken. Add
`--endpoint opc.tcp://HOST:48012` to every command below if it is not local.

### 4. Debug the CAN bus (only if step 5 comes back empty)

Watch the traffic passively. You should see the server's SYNC and node-guard
requests going out, and replies from `0x7FF & (0x700 + 63)` = `73F` coming back.

```bash
candump -ta can8
```

If nothing at all comes back, stop the server and probe every node id directly —
this also catches a crate whose node id was changed from the default 63.

```bash
./elmbpsu_can.py --iface can8 scan
```

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

### Done

```bash
./elmbpsu_opcua.py off all
```

---

If a switch reports `READ-BACK MISMATCH`, or `mon` shows 0 V while the state
says ON, see the troubleshooting ladder in `README.md` §6.
