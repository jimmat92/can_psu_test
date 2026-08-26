#!/usr/bin/env python3
"""Offline self-test: simulates an ELMB responder so the CANopen encode/decode,
branch mapping and unit conversions can be exercised without hardware."""
import os, struct, sys

# Import the module under test from ../lib, whatever the current directory is.
# setup.sh also drops a .pth into .venv so "import elmbpsu_can" works after
# activation; this fallback keeps the test runnable without the venv.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "lib"))
import elmbpsu_can as t

NODE = 63

class FakeElmbBus:
    """Answers SDO requests like an ELMBio with ports A and C as outputs."""
    def __init__(self):
        self.port_c = 0x00
        self.port_a = 0x00
        self.rpdo_frames = []
        self.od = {(0x1009, 0): (4, 0x00010002),
                   (0x100A, 0): (4, int.from_bytes(b"MA44", "little")),
                   (0x100A, 1): (4, int.from_bytes(b"6ELM", "little")),
                   (0x3100, 0): (4, 123456),
                   (0x100C, 0): (2, 10000),
                   (0x100D, 0): (1, 3),
                   (0x2300, 0): (1, 0x00),
                   (0x6208, 1): (1, 0xFF),
                   (0x6208, 2): (1, 0xFF)}
        self._pending = None
    def send(self, cob, data=b"", rtr=False):
        if cob == t.NMT_ERR_BASE + NODE and rtr:
            self._pending = (cob, bytes([0x05]), False); return
        if cob == t.RPDO1_BASE + NODE:
            self.rpdo_frames.append(data)
            self.port_c, self.port_a = data[0], data[1]; return
        if cob == t.NMT_COB:
            return
        if cob != t.SDO_RX_BASE + NODE:
            return
        cmd, index, sub = struct.unpack("<BHB", data[:4])
        tx = t.SDO_TX_BASE + NODE
        if (cmd & 0xE0) == 0x40:                       # upload
            if (index, sub) == (0x6200, 1): size, val = 1, self.port_c
            elif (index, sub) == (0x6200, 2): size, val = 1, self.port_a
            elif index == 0x2404:                      # AI: 12.0 V / 20 mA branch
                ch = sub - 1
                val = 120000 if ch % 8 < 4 else 2502500   # 0-3 volts, 4-7 currents
                self._pending = (tx, struct.pack("<BHB i", 0x43, index, sub, val), False); return
            elif (index, sub) in self.od: size, val = self.od[(index, sub)]
            else:
                self._pending = (tx, struct.pack("<BHB I", 0x80, index, sub, 0x06020000), False); return
            scs = 0x43 | ((4 - size) << 2)
            self._pending = (tx, struct.pack("<BHB", scs, index, sub)
                             + val.to_bytes(4, "little"), False); return
        if (cmd & 0xE0) == 0x20:                       # download
            n = (cmd >> 2) & 3; size = 4 - n
            val = int.from_bytes(data[4:4 + size], "little")
            if index == 0x6220:
                bit = sub - 1
                if bit < 8:
                    self.port_c = (self.port_c | (1 << bit)) if val else (self.port_c & ~(1 << bit))
                else:
                    bit -= 8
                    self.port_a = (self.port_a | (1 << bit)) if val else (self.port_a & ~(1 << bit))
            else:
                self.od[(index, sub)] = (size, val)
            self._pending = (tx, struct.pack("<BHB4x", 0x60, index, sub), False)
    def recv(self, deadline):
        p, self._pending = self._pending, None; return p
    def wait_for(self, cob, timeout):
        got = self.recv(0)
        return got[1] if got and got[0] == cob else None

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok: fails.append(label)

bus = FakeElmbBus()
elmb = t.Elmb(bus, NODE)

print("SDO reads")
check("hwVersion", elmb.read_named("hwVersion"), 0x00010002)
check("serialNumber", elmb.read_named("serialNumber"), 123456)
check("dioOutputMaskC", elmb.read_named("dioOutputMaskC"), 0xFF)
check("swVersion bytes", elmb.read_named("swVersion").to_bytes(4, "little"), b"MA44")

print("branch -> bit mapping via RPDO1 (byte0=portC, byte1=portA)")
elmb.write_do_word_rpdo(1 << 0)
check("branch 0  -> portC bit0", (bus.port_c, bus.port_a), (0x01, 0x00))
elmb.write_do_word_rpdo(1 << 8)
check("branch 8  -> portA bit0", (bus.port_c, bus.port_a), (0x00, 0x01))
elmb.write_do_word_rpdo(1 << 15)
check("branch 15 -> portA bit7", (bus.port_c, bus.port_a), (0x00, 0x80))
elmb.write_do_word_rpdo(0xFFFF)
check("all on read-back word", elmb.read_do_word(), 0xFFFF)

print("branch -> bit mapping via bitwise SDO 0x6220")
elmb.write_do_word_rpdo(0x0000)
elmb.write_do_bit_sdo(3, 1)
check("branch 3  -> portC bit3", (bus.port_c, bus.port_a), (0x08, 0x00))
elmb.write_do_bit_sdo(9, 1)
check("branch 9  -> portA bit1", (bus.port_c, bus.port_a), (0x08, 0x02))
check("word read-back", elmb.read_do_word(), (1 << 3) | (1 << 9))

print("analog-input channel map (fwElmbPSU_createMonitorChannel)")
expected = {0: (0, 1, 4, 5), 1: (2, 3, 6, 7), 2: (8, 9, 12, 13),
            3: (10, 11, 14, 15), 15: (58, 59, 62, 63)}
for b, want in expected.items():
    base = (b * 4) - (2 * (b % 2))
    check(f"branch {b} (CANV,ADV,CANI,ADI)", (base, base + 1, base + 4, base + 5), want)
check("all 64 channels used exactly once",
      sorted(c for b in range(16) for c in
             ((b * 4) - (2 * (b % 2)) + o for o in (0, 1, 4, 5))), list(range(64)))

print("sensor conversions (fwElmbPSU.postInstall formulas)")
uv = elmb.read_ai_uv(0)
check("CAN voltage from 120000 uV", round(uv / 1e6 * 100.0, 3), 12.0)
uv = elmb.read_ai_uv(4)
check("CAN current from 2502500 uV", round((uv / 1e6 - 2.5) * 5.0 / 0.625, 4), 0.02)

print("branch <-> slot/position labels")
check("branch 0", t.branch_label(0), "branch  0 (slot 0, position A)")
check("branch 1", t.branch_label(1), "branch  1 (slot 0, position B)")
check("branch 14", t.branch_label(14), "branch 14 (slot 7, position A)")
check("branch 15", t.branch_label(15), "branch 15 (slot 7, position B)")

print("branch spec parsing")
check("'all'", t._parse_branches("all"), list(range(16)))
check("'0,2,4'", t._parse_branches("0,2,4"), [0, 2, 4])
check("'0-3'", t._parse_branches("0-3"), [0, 1, 2, 3])

print()
print(f"{'ALL TESTS PASSED' if not fails else str(len(fails)) + ' FAILURE(S): ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
