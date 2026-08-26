# Configuration

Two files, both read by `CanOpenOpcUa` at startup. Neither is read by
`tests/can_diag.py` or by the tools in [../lib/](../lib/) — those take
everything on the command line.

```bash
sudo /opt/labTempMonitor/bin/CanOpenOpcUa \
    --config_file          $PWD/config/config-elmbpsu.xml \
    --opcua_backend_config $PWD/config/ServerConfig-elmbpsu.xml
```

Both flags are mandatory: `--opcua_backend_config` defaults to the
`ServerConfig.xml` sitting next to the *binary*, not to your working directory.

## `config-elmbpsu.xml` — the crate

What the server talks to, and what it publishes. Self-contained: it does not
pull in the `CANopen_def_STDELMB_*.xmle` entity files, so there is no DTD path
to get wrong. **The four values you actually change:**

| where | attribute | current | change it when |
|-------|-----------|---------|----------------|
| `<Bus>` | `port` | `can13` | you use a different CAN interface. Pick one `../tests/can_diag.py` reports as FREE. |
| `<Bus>` | `settings` | `125k` | the crate is not at the 125 kbit/s ELMB PSU default. Must be a real bitrate — see below. |
| `<Node>` | `id` | `57` | you move to a different crate. **Confirm with a bus scan, never assume.** 63 is only the factory default; this crate is not on it. |
| `<Bus>` | `syncIntervalMs` | `10000` | you want faster analog sampling. TPDO3 delivers one full 64-channel scan per SYNC, so this is the `mon --source tpdo` refresh period. Drop to ~1000 to watch a fast-changing rail. |

```bash
../tests/can_diag.py                            # which port is free
../lib/elmbpsu_can.py --iface can13 scan        # which node id answers
```

`<Bus name>` (`psuCtrlBus`) and `<Node name>` (`psuCrate1`) are labels only, but
they become the first two elements of every OPC-UA node id, so
`../lib/elmbpsu_opcua.py` needs `--bus` / `--node` if you rename them.

**`settings="125k"` requires privileges, and `DontConfigure` is not a way out** —
it fails `EINVAL` and **leaves your port DOWN**. Run the server under `sudo`, or
give a private copy `CAP_NET_ADMIN`. Full explanation in
[../docs/REFERENCE.md](../docs/REFERENCE.md) §6.

Other bus providers: for an AnaGate set `provider="an"` and put the AnaGate
address in `port`. `provider="sock"` is SocketCAN.

## `ServerConfig-elmbpsu.xml` — the OPC-UA endpoint

A copy of `/opt/labTempMonitor/bin/ServerConfig.xml` with **one line changed**:

```xml
<Url>opc.tcp://[NodeName]:48012</Url>     <!-- was 33815 -->
```

33815 is **not** an option — the lab temperature monitor has held it since July.
If 48012 is also taken (check with `../tests/can_diag.py`), pick another port
here.

`[NodeName]` is a placeholder the server substitutes with the host name.
Everything else is stock: security policy `None`,
`<EnableAnonymous>true</EnableAnonymous>`, and the PKI paths under
`/localdisk/tmp/PKI/` already exist and are world-writable, so there is no
certificate work to do.

## Checking a change

`setup.sh` verifies both files are well-formed. By hand:

```bash
python3 -c "import xml.dom.minidom as m; m.parse('config/config-elmbpsu.xml')"
```

`config-elmbpsu.xml` also validates against
`/opt/CanOpenOpcUa/Configuration/Configuration.xsd`. One trap when editing its
comments: a double hyphen cannot appear inside an XML comment, so command-line
flags are spelled out in prose there rather than written with their leading
dashes.
