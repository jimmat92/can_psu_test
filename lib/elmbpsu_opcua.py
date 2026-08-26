#!/usr/bin/env python3
"""
OPC-UA client for controlling an ELMB PSU crate through the CERN
CanOpenOpcUa server -- a stand-in for WinCC OA + fwElmbPSU + fwElmb.

Requires:  asyncua.  ./setup.sh builds a .venv with it; then
           "source .venv/bin/activate" puts this script on PATH as
           "elmbpsu-opcua".
Server:    sudo /opt/labTempMonitor/bin/CanOpenOpcUa
             --config_file          $CAN_PSU_CONFIG/config-elmbpsu.xml
             --opcua_backend_config $CAN_PSU_CONFIG/ServerConfig-elmbpsu.xml
           Both flags are mandatory; see config/README.md.

Address-space layout produced by that config (quasar builds string node ids
by dot-joining the object hierarchy under the Objects folder):

    <bus>.<node>.RPDO1.do_write            UInt16, the whole 16-bit DO word
    <bus>.<node>.RPDO1.branch00 .. 15      Boolean, one per branch
    <bus>.<node>.do_bitwise.do_C_0 ...     Boolean, bitwise SDO writes
    <bus>.<node>.do_read.do_C_read         Byte,   read-back of port C
    <bus>.<node>.do_read.do_A_read         Byte,   read-back of port A
    <bus>.<node>.dioOutputMask.dioOutputMaskC / ...A
    <bus>.<node>.doInitHigh                Byte,   power-up output state
    <bus>.<node>.stateAsText               String, CANopen NMT state
    <bus>.<node>.aisdo.aisdo_0 .. 63       Int32,  on-request analog inputs
    <bus>.<node>.TPDO3.ch0.value           Int32,  SYNC-driven analog inputs
    <bus>.<node>.TPDO3.ch0.adcFlag         Byte

Branch numbering and the bit map are identical to elmbpsu_can.py; see the
header of that file for where each fact comes from in the JCOP sources.
"""

import argparse
import sys
import time

try:
    from asyncua.sync import Client
    from asyncua import ua
except ImportError:
    sys.exit("error: the 'asyncua' package is required.\n"
             "       pip install asyncua")

NS_URI = "OPCUASERVER"


def branch_label(branch):
    return f"branch {branch:2d} (slot {branch // 2}, position {'AB'[branch % 2]})"


def parse_branches(spec):
    if spec == "all":
        return list(range(16))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-")
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    for b in out:
        if not 0 <= b <= 15:
            sys.exit(f"error: branch {b} out of range 0..15")
    return out


class PsuCrate:
    def __init__(self, client, ns, bus, node):
        self.client = client
        self.ns = ns
        self.prefix = f"{bus}.{node}"

    def node_at(self, path):
        return self.client.get_node(ua.NodeId(f"{self.prefix}.{path}", self.ns))

    def read(self, path):
        return self.node_at(path).read_value()

    def state(self):
        return self.read("stateAsText")

    def do_word(self):
        return (self.read("do_read.do_A_read") << 8) | self.read("do_read.do_C_read")

    def write_word(self, word):
        self.node_at("RPDO1.do_write").write_value(
            ua.DataValue(ua.Variant(int(word) & 0xFFFF, ua.VariantType.UInt16)))

    def write_branch(self, branch, value):
        self.node_at(f"RPDO1.branch{branch:02d}").write_value(
            ua.DataValue(ua.Variant(bool(value), ua.VariantType.Boolean)))

    def write_branch_sdo(self, branch, value):
        port, bit = ("C", branch) if branch < 8 else ("A", branch - 8)
        self.node_at(f"do_bitwise.do_{port}_{bit}").write_value(
            ua.DataValue(ua.Variant(bool(value), ua.VariantType.Boolean)))

    def ai_uv(self, channel, source):
        if source == "sdo":
            return self.read(f"aisdo.aisdo_{channel}")
        return self.read(f"TPDO3.ch{channel}.value")

    def ping(self):
        """Round-trip through the address space config.xml built, not raw
        CANopen: read stateAsText, the cheapest node every crate config
        publishes. Success here means the OPC-UA server, the XML config's
        node-id naming (<bus>.<node>...), and the CAN link to the ELMB are
        all working end to end. Returns (ok, state_or_error)."""
        try:
            return True, self.state()
        except Exception as exc:
            return False, str(exc)


def show_branches(word, on_value):
    for b in range(16):
        bit = (word >> b) & 1
        print(f"    {branch_label(b)}: {'ON ' if bit == on_value else 'OFF'}"
              f"   [bit {b} = {bit}]")


def cmd_ping(crate, args):
    ok, result = crate.ping()
    if not ok:
        print(f"PING FAILED  {crate.prefix} @ {args.endpoint}: {result}")
        return 1
    print(f"PING OK      {crate.prefix} @ {args.endpoint}  stateAsText={result!r}")
    return 0


def cmd_status(crate, args):
    print(f"--- {crate.prefix} @ {args.endpoint} ---")
    print(f"NMT state       : {crate.state()}")
    for path, label in (("dioOutputMask.dioOutputMaskC", "dioOutputMaskC"),
                        ("dioOutputMask.dioOutputMaskA", "dioOutputMaskA"),
                        ("doInitHigh", "doInitHigh"),
                        ("serialNumber", "serialNumber"),
                        ("hwVersion", "hwVersion")):
        try:
            val = crate.read(path)
            print(f"{label:16s}: {val}  (0x{val:X})")
        except Exception as exc:
            print(f"{label:16s}: read failed: {exc}")
    word = crate.do_word()
    on_value = 0 if args.invert else 1
    print(f"DO word         : 0x{word:04X}  (portA=0x{word >> 8:02X} "
          f"portC=0x{word & 0xFF:02X})")
    print("branch states:")
    show_branches(word, on_value)
    return 0


def cmd_switch(crate, args, turn_on):
    branches = parse_branches(args.branches)
    on_value = 0 if args.invert else 1
    level = on_value if turn_on else (1 - on_value)

    state = crate.state()
    if args.method == "rpdo" and state != "OPERATIONAL":
        print(f"warning: node is {state}, RPDO writes are only acted on in "
              f"OPERATIONAL. Set requestedState, or use --method sdo.")

    before = crate.do_word()
    want = before
    for b in branches:
        want = (want | (1 << b)) if level else (want & ~(1 << b))
    print(f"switching {'ON' if turn_on else 'OFF'}: "
          f"{', '.join(str(b) for b in branches)}")
    print(f"  DO word 0x{before:04X} -> 0x{want:04X}   (method: {args.method})")

    if args.method == "rpdo":
        # Write the whole 16-bit word, not the per-branch Boolean nodes.
        #
        # RPDO1.branchNN is a quasar RpdoCachedVariable: writing it read-modify-
        # writes the *server's* 8-byte RPDO shadow cache and transmits all of it
        # (Device/src/DRpdoCachedVariable.cpp writeValue -> propagateCache). That
        # cache starts at all zeros (DRpdo.cpp m_cache.assign(8, 0)) and knows
        # nothing about the state the ELMB powered up in from doInitHigh (0x2300).
        # So on a freshly started server the first per-branch write transmits the
        # cache, not the intended word, and silently switches every other branch
        # off. Writing do_write overwrites the cache wholesale, so intent and
        # cache always agree. The branchNN nodes remain in the address space for
        # other clients that keep their own state.
        crate.write_word(want)
    else:
        for b in branches:
            crate.write_branch_sdo(b, level)

    time.sleep(args.settle)
    after = crate.do_word()
    print(f"  read back    0x{after:04X}")
    if after != want:
        print("  *** READ-BACK MISMATCH ***  see the troubleshooting ladder in README.md s6")
        if args.method == "sdo" and before != 0 and after == 0:
            print("  (all bits cleared: the server's RPDO cache was stale. It is "
                  "now in sync;\n   re-issue the command, or use --method rpdo.)")
        return 1
    print("  read-back OK")
    show_branches(after, on_value)
    return 0


def cmd_word(crate, args):
    crate.write_word(args.value)
    time.sleep(args.settle)
    after = crate.do_word()
    print(f"wrote 0x{args.value:04X}, read back 0x{after:04X}"
          f"{'' if after == args.value else '   *** MISMATCH ***'}")
    return 0 if after == args.value else 1


def cmd_mon(crate, args):
    branches = parse_branches(args.branches)
    print(f"source: {args.source}")
    print(f"{'branch':>6} {'CAN V':>9} {'CAN I':>10} {'AD V':>9} {'AD I':>10}")
    for b in branches:
        base = (b * 4) - (2 * (b % 2))
        cells = []
        for channel, is_volt in ((base, True), (base + 4, False),
                                 (base + 1, True), (base + 5, False)):
            try:
                volts = crate.ai_uv(channel, args.source) / 1e6
                val = volts * 100.0 if is_volt else (volts - 2.5) * 5.0 / 0.625
                cells.append(f"{val:8.3f}{'V' if is_volt else 'A'}")
            except Exception:
                cells.append("     n/a ")
        print(f"{b:>6} {cells[0]} {cells[1]} {cells[2]} {cells[3]}")
    return 0


def cmd_browse(crate, args):
    def walk(node, depth=0):
        if depth > args.depth:
            return
        for child in node.get_children():
            try:
                name = child.read_browse_name().Name
                nid = child.nodeid.Identifier
                cls = child.read_node_class().name
                value = ""
                if cls == "Variable":
                    try:
                        value = f" = {child.read_value()!r}"
                    except Exception:
                        value = " = <unreadable>"
                print("  " * depth + f"{name}  [{nid}] {cls}{value}")
                walk(child, depth + 1)
            except Exception as exc:
                print("  " * depth + f"<error: {exc}>")
    walk(crate.client.get_node(ua.NodeId(crate.prefix.split(".")[0], crate.ns)))
    return 0


def main():
    p = argparse.ArgumentParser(
        description="Control an ELMB PSU crate via the CanOpenOpcUa OPC-UA server.")
    p.add_argument("--endpoint", default="opc.tcp://localhost:48012")
    p.add_argument("--bus", default="psuCtrlBus", help="Bus name in config/config-elmbpsu.xml")
    p.add_argument("--node", default="psuCrate1", help="Node name in config/config-elmbpsu.xml")
    p.add_argument("--invert", action="store_true",
                   help="old (pre-2.0.0) PSU: output level 0 means ON")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="cheapest possible read, to confirm the server "
                    "+ config.xml + CAN link are all up (no low-level CANopen)")
    sub.add_parser("status", help="NMT state, DO configuration, branch states")

    for name, doc in (("on", "switch branches ON"), ("off", "switch branches OFF")):
        s = sub.add_parser(name, help=doc)
        s.add_argument("branches", help="e.g. 0  |  0,1  |  0-3  |  all")
        s.add_argument("--method", choices=["rpdo", "sdo"], default="rpdo")
        s.add_argument("--settle", type=float, default=0.5)

    s = sub.add_parser("word", help="write the raw 16-bit DO word")
    s.add_argument("value", type=lambda x: int(x, 0))
    s.add_argument("--settle", type=float, default=0.5)

    s = sub.add_parser("mon", help="branch voltages and currents")
    s.add_argument("--branches", default="all")
    # tpdo is the default because the on-request 0x2404 reads return Bad on the
    # crate this was commissioned against (ELMB sw "MA43"), while the SYNC-driven
    # TPDO3 scan works. Keep sdo available: it needs no SYNC and is the fallback
    # if a crate has aiTransmissionType != 1.
    s.add_argument("--source", choices=["tpdo", "sdo"], default="tpdo",
                   help="tpdo = SYNC-driven TPDO3 cache (default); sdo = on-request 0x2404")

    s = sub.add_parser("browse", help="dump the server address space")
    s.add_argument("--depth", type=int, default=3)

    args = p.parse_args()

    client = Client(url=args.endpoint)
    try:
        client.connect()
    except Exception as exc:
        sys.exit(f"error: cannot connect to {args.endpoint}: {exc}")
    try:
        try:
            ns = client.get_namespace_index(NS_URI)
        except Exception:
            ns = 2
            print(f"warning: namespace {NS_URI!r} not found, assuming ns={ns}")
        crate = PsuCrate(client, ns, args.bus, args.node)
        if args.cmd == "ping":
            return cmd_ping(crate, args)
        if args.cmd == "status":
            return cmd_status(crate, args)
        if args.cmd == "on":
            return cmd_switch(crate, args, True)
        if args.cmd == "off":
            return cmd_switch(crate, args, False)
        if args.cmd == "word":
            return cmd_word(crate, args)
        if args.cmd == "mon":
            return cmd_mon(crate, args)
        if args.cmd == "browse":
            return cmd_browse(crate, args)
        return 2
    finally:
        client.disconnect()


if __name__ == "__main__":
    sys.exit(main())
