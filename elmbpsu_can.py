#!/usr/bin/env python3
"""
Direct SocketCAN bring-up / test tool for the CERN ELMB PSU crate.

No third-party dependencies: uses the Linux SocketCAN support built into
CPython's socket module.  Intended for bench testing and diagnosis *before*
CanOpenOpcUa / WinCC OA are in the picture.

Object dictionary / mapping facts, all reverse-engineered from the JCOP
framework sources shipped alongside this file:

  fwElmbPSU/scripts/libs/fwElmbPSU/fwElmbPSU.ctl  fwElmbPSU_createPowerControl()
      branch 0..7   -> ELMB digital output port C, bit = branch
      branch 8..15  -> ELMB digital output port A, bit = branch - 8

  fwElmb/scripts/libs/fwElmb/fwElmbUser.ctl  fwElmbUser_setDoBits()/getDoBytes()
      the 16-bit DO word is (portA << 8) | portC
      => bit N of the word is branch N, for all 16 branches

  fwElmb/config/fwElmb/OPCUA_nodeType_ELMB.xml
      RPDO1 (cobid 0x200 + node) carries UInt16 "do_write" at byte offset 0
      SDO 0x6200:1 = do_C_read, 0x6200:2 = do_A_read
      SDO 0x6220:1..8   = do_C_0..7      (bitwise write)
      SDO 0x6220:9..16  = do_A_0..7
      SDO 0x6208:1 = dioOutputMaskC, 0x6208:2 = dioOutputMaskA
      SDO 0x2300   = doInitHigh (power-up state of the outputs)
      SDO 0x2404:1..0x40 = analog inputs 0..63, on-request, signed, microvolts

  fwElmbPSU/scripts/fwElmbPSU/fwElmbPSU.postInstall (sensor definitions)
      branch voltage [V] = raw_uV / 1e6 * 100.0
      branch current [A] = (raw_uV / 1e6 - 2.5) * 5.0 / 0.625

  fwElmbPSU/scripts/libs/fwElmbPSU/fwElmbPSUConstants.ctl
      production ("new") PSU:  1 = ON, 0 = OFF
      pre-2.0.0 ("old") PSU:   0 = ON, 1 = OFF   -> use --invert
"""

import argparse
import socket
import struct
import sys
import time

CAN_FRAME_FMT = "<IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(CAN_FRAME_FMT)
CAN_RTR_FLAG = 0x40000000
CAN_EFF_FLAG = 0x80000000
CAN_ERR_FLAG = 0x20000000

NMT_COB = 0x000
SYNC_COB = 0x080
EMCY_BASE = 0x080
TPDO1_BASE = 0x180
RPDO1_BASE = 0x200
TPDO3_BASE = 0x380
SDO_TX_BASE = 0x580   # node -> master
SDO_RX_BASE = 0x600   # master -> node
NMT_ERR_BASE = 0x700  # bootup / heartbeat / node guard

NMT_START = 0x01
NMT_STOP = 0x02
NMT_PREOP = 0x80
NMT_RESET_NODE = 0x81
NMT_RESET_COMM = 0x82

# ELMB object dictionary entries we care about
OD = {
    "hwVersion":      (0x1009, 0x00, 4),
    "swVersion":      (0x100A, 0x00, 4),
    "swMinorVersion": (0x100A, 0x01, 4),
    "guardTime":      (0x100C, 0x00, 2),
    "lifeTime":       (0x100D, 0x00, 1),
    "serialNumber":   (0x3100, 0x00, 4),
    "doInitHigh":     (0x2300, 0x00, 1),
    "do_C_read":      (0x6200, 0x01, 1),
    "do_A_read":      (0x6200, 0x02, 1),
    "dioOutputMaskC": (0x6208, 0x01, 1),
    "dioOutputMaskA": (0x6208, 0x02, 1),
    "aiChannelMax":   (0x2100, 0x01, 1),
    "aiRate":         (0x2100, 0x02, 1),
    "aiRange":        (0x2100, 0x03, 1),
    "aiMode":         (0x2100, 0x04, 1),
}

SDO_ABORT = {
    0x05040000: "SDO protocol timed out",
    0x05040001: "client/server command specifier not valid",
    0x06010000: "unsupported access to an object",
    0x06010001: "attempt to read a write-only object",
    0x06010002: "attempt to write a read-only object",
    0x06020000: "object does not exist in the object dictionary",
    0x06090011: "sub-index does not exist",
    0x06090030: "value range of parameter exceeded",
    0x08000000: "general error",
    0x08000020: "data cannot be transferred/stored to the application",
}


class CanBus:
    def __init__(self, iface, timeout=1.0, verbose=False):
        self.iface = iface
        self.verbose = verbose
        try:
            self.sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            self.sock.bind((iface,))
        except OSError as exc:
            sys.exit(f"error: cannot open CAN interface {iface!r}: {exc}\n"
                     f"       check 'ip link show {iface}' and that the link is UP")
        self.sock.settimeout(timeout)

    def send(self, cob_id, data=b"", rtr=False):
        can_id = cob_id | (CAN_RTR_FLAG if rtr else 0)
        dlc = 0 if rtr else len(data)
        frame = struct.pack(CAN_FRAME_FMT, can_id, dlc, data.ljust(8, b"\x00"))
        if self.verbose:
            print(f"  TX {cob_id:03X}#{'R' if rtr else data.hex().upper()}")
        self.sock.send(frame)

    def recv(self, deadline):
        """Return (cob_id, data, is_rtr) or None on timeout."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                frame = self.sock.recv(CAN_FRAME_SIZE)
            except (socket.timeout, TimeoutError):
                return None
            can_id, dlc, data = struct.unpack(CAN_FRAME_FMT, frame)
            if can_id & CAN_ERR_FLAG:
                continue
            cob = can_id & 0x7FF
            rtr = bool(can_id & CAN_RTR_FLAG)
            payload = data[:dlc]
            if self.verbose:
                print(f"  RX {cob:03X}#{'R' if rtr else payload.hex().upper()}")
            return cob, payload, rtr

    def wait_for(self, cob_id, timeout):
        deadline = time.monotonic() + timeout
        while True:
            got = self.recv(deadline)
            if got is None:
                return None
            if got[0] == cob_id and not got[2]:
                return got[1]


class SdoError(Exception):
    pass


class Elmb:
    def __init__(self, bus, node, timeout=1.0):
        self.bus = bus
        self.node = node
        self.timeout = timeout

    # ---- NMT ---------------------------------------------------------
    def nmt(self, command):
        self.bus.send(NMT_COB, bytes([command, self.node]))

    def guard(self, timeout=None):
        """Node-guard request; returns the raw state byte or None."""
        self.bus.send(NMT_ERR_BASE + self.node, rtr=True)
        data = self.bus.wait_for(NMT_ERR_BASE + self.node, timeout or self.timeout)
        return data[0] if data else None

    # ---- SDO ---------------------------------------------------------
    def sdo_upload(self, index, subindex, size):
        req = struct.pack("<BHB4x", 0x40, index, subindex)
        self.bus.send(SDO_RX_BASE + self.node, req)
        rsp = self.bus.wait_for(SDO_TX_BASE + self.node, self.timeout)
        if rsp is None:
            raise SdoError(f"timeout reading 0x{index:04X}:{subindex:02X} "
                           f"from node {self.node}")
        scs = rsp[0]
        if scs == 0x80:
            code = struct.unpack("<I", rsp[4:8])[0]
            raise SdoError(f"SDO abort 0x{code:08X} on 0x{index:04X}:{subindex:02X}"
                           f" ({SDO_ABORT.get(code, 'unknown abort code')})")
        if (scs & 0xE0) != 0x40:
            raise SdoError(f"unexpected SDO response 0x{scs:02X}")
        if not scs & 0x02:
            raise SdoError("segmented SDO upload not supported by this tool")
        n = (scs >> 2) & 0x03
        nbytes = (4 - n) if (scs & 0x01) else size
        return rsp[4:4 + nbytes]

    def sdo_download(self, index, subindex, value, size):
        payload = int(value).to_bytes(size, "little", signed=False)
        cmd = 0x23 | ((4 - size) << 2)
        req = struct.pack("<BHB", cmd, index, subindex) + payload.ljust(4, b"\x00")
        self.bus.send(SDO_RX_BASE + self.node, req)
        rsp = self.bus.wait_for(SDO_TX_BASE + self.node, self.timeout)
        if rsp is None:
            raise SdoError(f"timeout writing 0x{index:04X}:{subindex:02X} "
                           f"on node {self.node}")
        if rsp[0] == 0x80:
            code = struct.unpack("<I", rsp[4:8])[0]
            raise SdoError(f"SDO abort 0x{code:08X} on 0x{index:04X}:{subindex:02X}"
                           f" ({SDO_ABORT.get(code, 'unknown abort code')})")
        if rsp[0] != 0x60:
            raise SdoError(f"unexpected SDO download response 0x{rsp[0]:02X}")

    def read_named(self, name):
        index, sub, size = OD[name]
        raw = self.sdo_upload(index, sub, size)
        return int.from_bytes(raw, "little", signed=False)

    def read_ai_uv(self, channel):
        """Analog input via SDO 0x2404 (on request). Returns signed microvolts."""
        raw = self.sdo_upload(0x2404, channel + 1, 4)
        return int.from_bytes(raw, "little", signed=True)

    # ---- digital outputs --------------------------------------------
    def read_do_word(self):
        port_c = self.read_named("do_C_read")
        port_a = self.read_named("do_A_read")
        return (port_a << 8) | port_c

    def write_do_word_rpdo(self, word):
        """The path fwElmbPSU actually uses. Requires node OPERATIONAL."""
        data = bytes([word & 0xFF, (word >> 8) & 0xFF])
        self.bus.send(RPDO1_BASE + self.node, data)

    def write_do_bit_sdo(self, branch, value):
        """Bitwise SDO write, works in PRE-OPERATIONAL too."""
        self.sdo_download(0x6220, branch + 1, 1 if value else 0, 1)


def branch_label(branch):
    return f"branch {branch:2d} (slot {branch // 2}, position {'AB'[branch % 2]})"


def decode_word(word, on_value):
    lines = []
    for b in range(16):
        bit = (word >> b) & 1
        state = "ON " if bit == on_value else "OFF"
        lines.append(f"    {branch_label(b)}: {state}   [bit {b} = {bit}]")
    return "\n".join(lines)


# ---------------------------------------------------------------- commands
def cmd_scan(args, bus):
    print(f"Probing node-guard on {args.iface} for node IDs 1..127 ...")
    found = []
    for node in range(1, 128):
        bus.send(NMT_ERR_BASE + node, rtr=True)
        data = bus.wait_for(NMT_ERR_BASE + node, args.scan_timeout)
        if data:
            state = data[0] & 0x7F
            names = {0: "BOOTUP", 4: "STOPPED", 5: "OPERATIONAL", 127: "PRE-OPERATIONAL"}
            print(f"  node {node:3d} (0x{node:02X}): {names.get(state, hex(state))}")
            found.append(node)
    if not found:
        print("  no nodes answered.")
        print("  -> check bitrate (PSU control ELMB default is 125 kbit/s),")
        print("     CAN-H/CAN-L wiring and the 120 ohm terminators on the control bus")
        print("     (the control bus has NO built-in terminators - you must supply them).")
    return 0 if found else 1


def cmd_info(args, elmb):
    state = elmb.guard()
    names = {0: "BOOTUP", 4: "STOPPED", 5: "OPERATIONAL", 127: "PRE-OPERATIONAL"}
    if state is None:
        print(f"node {args.node} did not answer node guarding")
        return 1
    print(f"node {args.node} (0x{args.node:02X}) state: "
          f"{names.get(state & 0x7F, hex(state))}")
    for name in ("hwVersion", "swVersion", "swMinorVersion", "serialNumber",
                 "guardTime", "lifeTime"):
        try:
            val = elmb.read_named(name)
            extra = ""
            if name in ("swVersion", "swMinorVersion"):
                extra = "  " + repr(val.to_bytes(4, "little"))
            print(f"  {name:16s} = {val} (0x{val:X}){extra}")
        except SdoError as exc:
            print(f"  {name:16s} : {exc}")
    return 0


def cmd_status(args, elmb):
    print(f"--- ELMB PSU crate, control ELMB node {args.node} "
          f"(0x{args.node:02X}) on {args.iface} ---")
    state = elmb.guard()
    names = {0: "BOOTUP", 4: "STOPPED", 5: "OPERATIONAL", 127: "PRE-OPERATIONAL"}
    print(f"NMT state       : {names.get((state or 0) & 0x7F, 'NO REPLY')}")
    for name in ("dioOutputMaskC", "dioOutputMaskA", "doInitHigh"):
        try:
            val = elmb.read_named(name)
            print(f"{name:16s}: 0x{val:02X}  0b{val:08b}")
        except SdoError as exc:
            print(f"{name:16s}: {exc}")
    try:
        word = elmb.read_do_word()
    except SdoError as exc:
        print(f"digital outputs : {exc}")
        return 1
    on_value = 0 if args.invert else 1
    print(f"DO word         : 0x{word:04X}  (portA=0x{word >> 8:02X} "
          f"portC=0x{word & 0xFF:02X})")
    print(f"ON level        : {on_value}  "
          f"({'old/pre-2.0.0 PSU, inverted' if args.invert else 'production PSU'})")
    print("branch states:")
    print(decode_word(word, on_value))
    return 0


def cmd_nmt(args, elmb):
    mapping = {"start": NMT_START, "operational": NMT_START, "stop": NMT_STOP,
               "preop": NMT_PREOP, "reset": NMT_RESET_NODE, "resetcomm": NMT_RESET_COMM}
    elmb.nmt(mapping[args.command])
    print(f"sent NMT {args.command} to node {args.node}")
    time.sleep(0.3)
    state = elmb.guard()
    names = {0: "BOOTUP", 4: "STOPPED", 5: "OPERATIONAL", 127: "PRE-OPERATIONAL"}
    print(f"state now: {names.get((state or 0) & 0x7F, 'NO REPLY')}")
    return 0


def _parse_branches(spec):
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


def cmd_switch(args, elmb, turn_on):
    branches = _parse_branches(args.branches)
    on_value = 0 if args.invert else 1
    level = on_value if turn_on else (1 - on_value)

    try:
        before = elmb.read_do_word()
    except SdoError as exc:
        sys.exit(f"error: cannot read current output state: {exc}")
    word = before
    for b in branches:
        if level:
            word |= 1 << b
        else:
            word &= ~(1 << b)

    print(f"switching {'ON' if turn_on else 'OFF'}: "
          f"{', '.join(str(b) for b in branches)}")
    print(f"  DO word 0x{before:04X} -> 0x{word:04X}   (method: {args.method})")

    if args.method == "rpdo":
        state = elmb.guard()
        if state is not None and (state & 0x7F) != 5:
            print("  node is not OPERATIONAL - sending NMT start first")
            elmb.nmt(NMT_START)
            time.sleep(0.3)
        elmb.write_do_word_rpdo(word)
    else:
        for b in branches:
            elmb.write_do_bit_sdo(b, level)

    time.sleep(args.settle)
    try:
        after = elmb.read_do_word()
    except SdoError as exc:
        sys.exit(f"error: cannot read back output state: {exc}")
    print(f"  read back    0x{after:04X}")
    if after != word:
        print("  *** READ-BACK MISMATCH ***")
        print("  The ELMB did not latch what was written. Likely causes:")
        print("   - ports A/C are not configured as outputs "
              "(check dioOutputMaskC/A in 'status')")
        print("   - RPDO1 byte order is reversed on this firmware "
              "(retry with --method sdo)")
        print("   - node was not OPERATIONAL when the RPDO was sent")
        return 1
    print("  read-back OK")
    print(decode_word(after, on_value))
    return 0


def cmd_mon(args, elmb):
    branches = _parse_branches(args.branches)
    print(f"{'branch':>6} {'CAN V':>9} {'CAN I':>10} {'AD V':>9} {'AD I':>10}")
    for b in branches:
        base = (b * 4) - (2 * (b % 2))
        ch = {"canv": base, "adv": base + 1, "cani": base + 4, "adi": base + 5}
        vals = {}
        for key, channel in ch.items():
            try:
                uv = elmb.read_ai_uv(channel)
                volts_at_adc = uv / 1e6
                if key.endswith("v"):
                    vals[key] = volts_at_adc * 100.0
                else:
                    vals[key] = (volts_at_adc - 2.5) * 5.0 / 0.625
            except SdoError as exc:
                vals[key] = None
                if args.verbose:
                    print(f"  ai {channel}: {exc}")
        def fmt(key, unit):
            v = vals.get(key)
            return "     n/a " if v is None else f"{v:8.3f}{unit}"
        print(f"{b:>6} {fmt('canv','V')} {fmt('cani','A')} "
              f"{fmt('adv','V')} {fmt('adi','A')}")
    return 0


def cmd_dump(args, bus):
    print(f"listening on {args.iface} for {args.seconds}s (Ctrl-C to stop) ...")
    deadline = time.monotonic() + args.seconds
    count = 0
    try:
        while time.monotonic() < deadline:
            got = bus.recv(deadline)
            if got is None:
                break
            cob, data, rtr = got
            fn = cob & 0x780
            node = cob & 0x7F
            kind = {0x000: "NMT/SYNC", 0x080: "EMCY/SYNC", 0x180: "TPDO1",
                    0x200: "RPDO1", 0x280: "TPDO2", 0x300: "RPDO2",
                    0x380: "TPDO3", 0x400: "RPDO3", 0x480: "TPDO4",
                    0x500: "RPDO4", 0x580: "SDO-tx", 0x600: "SDO-rx",
                    0x700: "NMTERR"}.get(fn, "?")
            print(f"  {cob:03X} [{'R' if rtr else len(data)}] "
                  f"{data.hex(' ').upper():<24} {kind} node={node}")
            count += 1
    except KeyboardInterrupt:
        pass
    print(f"{count} frames")
    return 0


def cmd_outmask(args, elmb):
    print("Setting ELMB digital I/O direction masks (1 = output).")
    print(f"  0x6208:01 dioOutputMaskC = 0x{args.port_c:02X}")
    print(f"  0x6208:02 dioOutputMaskA = 0x{args.port_a:02X}")
    elmb.sdo_download(0x6208, 0x01, args.port_c, 1)
    elmb.sdo_download(0x6208, 0x02, args.port_a, 1)
    print(f"  read back C=0x{elmb.read_named('dioOutputMaskC'):02X} "
          f"A=0x{elmb.read_named('dioOutputMaskA'):02X}")
    if args.store:
        print("  storing parameters to EEPROM (0x1010:01 <- 'save')")
        elmb.sdo_download(0x1010, 0x01, int.from_bytes(b"save", "little"), 4)
        print("  stored")
    return 0


def main():
    p = argparse.ArgumentParser(
        description="Direct SocketCAN test tool for the CERN ELMB PSU crate.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Branch numbering: branch = 2*slot + (0 for position A/top, "
               "1 for position B/bottom); slot 0 is leftmost.")
    p.add_argument("--iface", default="can0", help="SocketCAN interface (default can0)")
    p.add_argument("--node", type=int, default=63,
                   help="control ELMB node id (default 63 = 0x3F)")
    p.add_argument("--timeout", type=float, default=1.0, help="SDO timeout in seconds")
    p.add_argument("--invert", action="store_true",
                   help="old (pre-2.0.0) PSU: output level 0 means ON")
    p.add_argument("-v", "--verbose", action="store_true", help="trace CAN frames")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="probe all node ids on the bus")
    s.add_argument("--scan-timeout", type=float, default=0.05)

    sub.add_parser("info", help="firmware / serial number of the control ELMB")
    sub.add_parser("status", help="read back branch on/off states and DO configuration")

    s = sub.add_parser("nmt", help="send an NMT command")
    s.add_argument("command", choices=["start", "operational", "stop", "preop",
                                       "reset", "resetcomm"])

    for name, doc in (("on", "switch branches ON"), ("off", "switch branches OFF")):
        s = sub.add_parser(name, help=doc)
        s.add_argument("branches", help="e.g. 0  |  0,1  |  0-3  |  all")
        s.add_argument("--method", choices=["rpdo", "sdo"], default="rpdo",
                       help="rpdo = what fwElmbPSU uses (needs OPERATIONAL); "
                            "sdo = bitwise 0x6220 (works in PRE-OPERATIONAL)")
        s.add_argument("--settle", type=float, default=0.3)

    s = sub.add_parser("mon", help="read branch voltages and currents")
    s.add_argument("--branches", default="all")

    s = sub.add_parser("dump", help="passive frame dump")
    s.add_argument("--seconds", type=float, default=10.0)

    s = sub.add_parser("outmask", help="configure ports A/C as outputs (0x6208)")
    s.add_argument("port_c", type=lambda x: int(x, 0))
    s.add_argument("port_a", type=lambda x: int(x, 0))
    s.add_argument("--store", action="store_true",
                   help="also persist to EEPROM via 0x1010:01")

    args = p.parse_args()
    bus = CanBus(args.iface, timeout=args.timeout, verbose=args.verbose)
    elmb = Elmb(bus, args.node, timeout=args.timeout)

    if args.cmd == "scan":
        return cmd_scan(args, bus)
    if args.cmd == "dump":
        return cmd_dump(args, bus)
    if args.cmd == "info":
        return cmd_info(args, elmb)
    if args.cmd == "status":
        return cmd_status(args, elmb)
    if args.cmd == "nmt":
        return cmd_nmt(args, elmb)
    if args.cmd == "on":
        return cmd_switch(args, elmb, True)
    if args.cmd == "off":
        return cmd_switch(args, elmb, False)
    if args.cmd == "mon":
        return cmd_mon(args, elmb)
    if args.cmd == "outmask":
        return cmd_outmask(args, elmb)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SdoError as exc:
        sys.exit(f"error: {exc}")
    except KeyboardInterrupt:
        sys.exit(130)
