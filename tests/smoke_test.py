#!/usr/bin/env python3
"""
End-to-end bring-up check for the ELMB PSU crate: start the CanOpenOpcUa
server, confirm something answers on the CAN bus, confirm the OPC-UA server
can talk to it through config-elmbpsu.xml, then stop the server.

    1. start the server (lib/elmbpsu_server.OpcUaServer)
    2. bus scan (lib/elmbpsu_can.scan_bus) -- print the node id(s) found,
       or terminate if the bus is empty
    3. ping (lib/elmbpsu_opcua.PsuCrate.ping) -- one read through the address
       space the XML config built, or terminate if it fails

The server is started as a context manager, so it is stopped on every exit
path: success, a failed scan, a failed ping, Ctrl-C, or any exception.

Defaults match config/config-elmbpsu.xml (Bus port="can13", Node id="57",
Bus name="psuCtrlBus", Node name="psuCrate1") and
config/ServerConfig-elmbpsu.xml (port 48012). Override with flags if you are
pointed at a different crate.
"""

import argparse
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

try:
    from elmbpsu_server import OpcUaServer, ServerError
    from elmbpsu_can import CanBus, scan_bus
    from elmbpsu_opcua import PsuCrate, NS_URI
    from asyncua.sync import Client
except ImportError as exc:
    sys.exit(f"error: cannot import required modules ({exc}).\n"
             "       activate the venv first: source .venv/bin/activate")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--iface", default="can13", help="CAN interface (Bus/@port)")
    p.add_argument("--scan-timeout", type=float, default=0.05,
                   help="per-node-id node-guard timeout for the scan")
    p.add_argument("--settle", type=float, default=1.0,
                   help="seconds to wait after the OPC-UA endpoint opens before "
                        "scanning, so CanModule has finished bringing up the link")
    p.add_argument("--endpoint", default=f"opc.tcp://{socket.gethostname()}:48012",
                   help="ServerConfig's <Url> substitutes [NodeName] with the "
                        "hostname (see server.log: 'Opened endpoint: "
                        "opc.tcp://<host>:48012'), which is not always reachable "
                        "at 'localhost' -- default here is this machine's own "
                        "hostname to match")
    p.add_argument("--bus", default="psuCtrlBus", help="Bus name in config-elmbpsu.xml")
    p.add_argument("--node", default="psuCrate1", help="Node name in config-elmbpsu.xml")
    p.add_argument("--config-file", default=None,
                   help="default: $CAN_PSU_CONFIG/config-elmbpsu.xml")
    p.add_argument("--opcua-backend-config", default=None,
                   help="default: $CAN_PSU_CONFIG/ServerConfig-elmbpsu.xml")
    p.add_argument("--no-sudo", action="store_true",
                   help="use if the binary already has CAP_NET_ADMIN (setcap)")
    p.add_argument("--start-timeout", type=float, default=15.0)
    p.add_argument("--warmup", type=float, default=30.0,
                   help="how long to let the server fetch stateAsText from the "
                        "ELMB before calling the ping failed")
    args = p.parse_args()

    server = OpcUaServer(config_file=args.config_file,
                         opcua_backend_config=args.opcua_backend_config,
                         use_sudo=not args.no_sudo)

    with server:
        print(f"[1/3] server started, pid {server.pid()}")
        print(f"      waiting up to {args.start_timeout:.0f}s for its OPC-UA "
              "endpoint to open (server.log) ...")
        if not server.wait_ready(timeout=args.start_timeout):
            sys.exit("error: OPC-UA endpoint never opened -- check server.log")
        print("      endpoint open")
        time.sleep(args.settle)

        print(f"[2/3] scanning {args.iface} for CANopen nodes ...")
        bus = CanBus(args.iface, timeout=1.0)
        found = scan_bus(bus, timeout=args.scan_timeout)
        if not found:
            sys.exit(f"error: bus scan found nothing on {args.iface} -- "
                     "check bitrate, wiring and termination")
        ids = ", ".join(str(node) for node, _ in found)
        print(f"      found node id(s): {ids}")

        print("[3/3] pinging the crate through the OPC-UA server ...")
        client = Client(url=args.endpoint)
        client.connect()
        try:
            try:
                ns = client.get_namespace_index(NS_URI)
            except Exception:
                ns = 2
            crate = PsuCrate(client, ns, args.bus, args.node)
            # Not a plain ping(): the server answers BadWaitingForInitialData
            # until it has read stateAsText from the ELMB once, and that waits
            # on a node-guard cycle (10 s in our config). The bus scan above
            # usually covers it, but only by accident.
            ok, result = crate.wait_ready(timeout=args.warmup)
        finally:
            client.disconnect()
        if not ok:
            sys.exit(f"error: ping failed: {result}")
        print(f"      ping OK, stateAsText={result!r}")

    print("done -- server stopped")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ServerError as exc:
        sys.exit(f"error: {exc}")
    except KeyboardInterrupt:
        sys.exit(130)
