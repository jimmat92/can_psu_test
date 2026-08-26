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
crate comes up with **all sixteen branches ON**. If a previous session left some
off, `elmbpsu-opcua on all` restores them.

### 6. Switch one branch on, then off

Branch 0 is slot 0, top connector. Each command re-reads the ELMB afterwards and
prints `read-back OK` only if the hardware really latched it.

```bash
elmbpsu-opcua on 0
elmbpsu-opcua off 0
```

**Do not try to confirm this with a multimeter.** The rails float and leave on
the module's rear-side connector, reaching the outside world through crate
cabling whose routing is installation-specific — step 7 is the check
([REFERENCE.md](REFERENCE.md) §5).

### 7. Read the voltage and current

```bash
elmbpsu-opcua on 0
elmbpsu-opcua mon --branches 0
```

Expect ~12 V on both rails and small currents (~20 mA CAN, ~25 mA AD) unloaded.
~0.01 V with about −19.8 A is an **empty slot**, not a fault: the current
transducer is in the module, so with no module nothing drives that input
([REFERENCE.md](REFERENCE.md) §4 has the full pattern table).

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
elmbpsu-cratescan -v                    # every table behind the verdict
elmbpsu-cratescan -n 20 --json scan.json
elmbpsu-cratescan --skip-switch-test    # read-only: switches nothing
elmbpsu-cratescan --use-running-server  # attach to a server already up
```

By default it prints a slot map and one line per module, nothing else:

```
  [0]  <-- populated
  [1]
  ...
  Module 0: OK
  Module 1: FAIL (fails to turn on: branches 2, 3 CAN V+AD V = 0.00 V)
  Module 2: FAIL (current sensor does not work: branches 4, 5 AD I = 0.31 V at the ADC pin)
  Module 3: UNKNOWN (on/off could not be judged -- the rails had not settled: ...)
```

Exit status: **0** nothing found, **1** something is faulty, **2** nothing
faulty but something could not be judged — which is not a pass.

Findings are **per slot, not per branch** — a module spans branches `2*slot`
and `2*slot+1`, so a fault on either condemns that one module. It checks:

- which slots hold a module, and which are empty;
- on/off, on the command path (DO read-back) and the physical one. At or below
  `--v-off-max` (1 V) is off; **anything above it has switched on**, healthy or
  not;
- the level: a commanded-on rail further than `--v-tol` (2 V) from
  `--v-nominal` (12 V) is an abnormal output, reported apart from failing to
  switch;
- whether each sensor reads something plausible *and* steadily. Both halves
  matter: a sense line that nothing drives sits rock steady at a few hundred
  mV, so a stdev test alone would pass it.

**A fault it names is a module fault** — the voltage divider and the current
transducer are both in the module, so the crate is not in the signal path.

**It waits for the rails to hold still after each switch**, rather than reading
the next scan. The ELMB sweeps its 64 inputs about once every 11 s, so a scan
taken straight after a switch holds pre-switch values — that is what makes a
healthy module look like it failed to switch ([REFERENCE.md](REFERENCE.md) §6).
Every wait is one sweep or more: `--settle-window` (12 s), `--sample-interval`
(12 s), `--settle-timeout` (48 s). A rail still moving when the wait ends is
reported as **unjudged**, per channel, not as a fault.

Presence is decided from the **current** inputs. A voltage input reads ~0
whether the branch is off or the slot is empty, but the module's transducer
holds its input at 2.5 V even with the branch switched off, and the input floats
only when there is no module. That is why the all-off step is a measurement and
not just a switch test ([REFERENCE.md](REFERENCE.md) §4).

**It power-cycles every branch.** Use `--skip-switch-test` if anything is
plugged in that should not be.

A default run is **~150 s** with the shipped config, and the two waits it is
made of are both real: the ELMB's sweep (five repeat scans one sweep apart is
60 s) and `syncIntervalMs`, which rounds every wait up to a whole SYNC — 10 s
each. Two levers: `-n 3` saves ~25 s, and setting `syncIntervalMs` to 1000 in
`config/config-elmbpsu.xml` ([../config/README.md](../config/README.md)) saves
~45 s. The sweep is the crate's own pace and cannot be shortened.

`--json` gets the per-channel mean and variance, the three state measurements
as engineering values, and every finding — not the raw samples.

---

If a switch reports `READ-BACK MISMATCH`, or `mon` shows 0 V while the state
says ON, work down the ladder in [../README.md](../README.md) §6. The first
entry — read-back `0x0000` when you asked for one branch — is the RPDO cache
trap, not a hardware fault.
