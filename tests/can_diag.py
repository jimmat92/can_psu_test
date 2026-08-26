#!/usr/bin/env python3
"""
Pre-flight diagnostics for the ELMB PSU bring-up on a *shared* machine.

Answers three questions before you touch the crate:

  1. Is anything else already running an OPC-UA server here, and on what port?
  2. What CAN interfaces exist, and what physical hardware is behind them?
  3. Which of them are in use by somebody else, and which are free to take?

This matters because two CANopen masters on one bus will fight over the same
node (docs/QUICKSTART.md step 3 warns about this), and because CanOpenOpcUa opens
the SocketCAN port exclusively enough that a second server on the same port is
a guaranteed bad time.

Stdlib only, Python 3.6+, no root required (root gives fuller attribution of
CAN sockets to processes -- see the coverage note in the output).

Where the facts come from:

  /sys/class/net/<if>/type == 280        ARPHRD_CAN, i.e. this is a CAN device
  ip -details -json link show            link state, CAN state, bitrate
  /sys/class/net/<if>/statistics/*       frame counters, sampled twice for a rate
  /sys/class/net/<if>/device             USB parent -> which physical adapter
  /proc/net/can/rcvlist_*                kernel receive filters == bound sockets
  lsof -> "protocol: CAN_RAW"            which processes hold CAN sockets
  /proc/net/tcp{,6}                      listening ports *and their owner uid*
                                         (ss/netstat hide the owner of another
                                         user's socket; the uid column does not)
  OPC-UA UACP HELLO handshake            positive proof a port speaks OPC-UA,
                                         per OPC 10000-6 section 7.1.2

CanOpenOpcUa's own endpoint is opc.tcp://<host>:48012 (CanOpenOpcUa/bin/
ServerConfig.xml); 4840 is the IANA-registered OPC-UA default.
"""

import argparse
import errno
import json
import os
import pwd
import re
import socket
import struct
import subprocess
import sys
import time

ARPHRD_CAN = 280

# Ports worth checking even when nothing is listening on them yet, plus the
# label to print when something is.
KNOWN_OPCUA_PORTS = {
    4840:  "OPC-UA default (IANA opcua-tcp)",
    4841:  "OPC-UA alternate",
    4842:  "OPC-UA alternate",
    4843:  "OPC-UA alternate (https)",
    26543: "OPC-UA (Beckhoff)",
    48010: "OPC-UA (Unified Automation demo)",
    48012: "CanOpenOpcUa default (bin/ServerConfig.xml)",
    48020: "OPC-UA (quasar servers)",
    48050: "OPC-UA alternate",
    51210: "OPC-UA (.NET reference server)",
    53530: "OPC-UA (Prosys)",
    62541: "OPC-UA (open62541 examples)",
}

# Listening ports we do not poke with a HELLO by default: sending them a
# malformed greeting is harmless but pointless and shows up in their logs.
PROBE_SKIP_RANGES = ((5900, 5999), (6000, 6099))  # VNC, X11
PROBE_MIN_PORT = 4000  # no OPC-UA server realistically lives below this

OPCUA_PROC_PATTERNS = (
    "opcua",        # catches CanOpenOpcUa, WCCOAopcua, python-opcua, ...
    "opc-ua",
    "open62541",
    "uaserver",
    "ua_server",
    "freeopcua",
    "asyncua",
    "prosys",
    "quasar",
)

CAN_PROC_PATTERNS = (
    "candump", "cansend", "cangen", "canplayer", "cansniffer", "canbusload",
    "canfdtest", "slcand", "elmbpsu_can", "python-can", "can.interface",
)

CONTAINER_PATTERNS = ("podman", "docker", "containerd-shim", "crun", "runc")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def read_text(path, default=""):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return default


def read_int(path, default=None):
    txt = read_text(path).strip()
    try:
        return int(txt)
    except ValueError:
        return default


def run(cmd, timeout=15):
    """Run a command, return (rc, stdout). Never raises."""
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=timeout)
        return proc.returncode, proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return -1, ""


def uid_name(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def human_rate(fps):
    if fps <= 0:
        return "0"
    if fps < 10:
        return "%.1f" % fps
    return "%d" % round(fps)


# --------------------------------------------------------------------------
# 1. OPC-UA servers
# --------------------------------------------------------------------------

def listening_tcp():
    """Every listening TCP socket, with the uid that owns it.

    Read straight out of /proc rather than via ss/netstat because those only
    name the process for sockets you own -- on a shared machine that hides
    exactly the servers you are looking for. The uid column is always there.
    """
    out = []
    for path, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        lines = read_text(path).splitlines()[1:]
        for line in lines:
            col = line.split()
            if len(col) < 10 or col[3] != "0A":  # 0A == TCP_LISTEN
                continue
            addr_hex, port_hex = col[1].rsplit(":", 1)
            out.append({
                "port": int(port_hex, 16),
                "addr": decode_addr(addr_hex, v6),
                "v6": v6,
                "uid": int(col[7]),
                "user": uid_name(int(col[7])),
                "inode": col[9],
            })
    out.sort(key=lambda e: (e["port"], e["v6"]))
    return out


def decode_addr(hex_addr, v6):
    """/proc/net/tcp stores the address as little-endian 32-bit words."""
    try:
        raw = bytes.fromhex(hex_addr)
    except ValueError:
        return "?"
    words = [raw[i:i + 4][::-1] for i in range(0, len(raw), 4)]
    packed = b"".join(words)
    try:
        if v6:
            return socket.inet_ntop(socket.AF_INET6, packed)
        return socket.inet_ntop(socket.AF_INET, packed)
    except (OSError, ValueError):
        return "?"


def endpoint_url(host, port):
    """opc.tcp://host:port, with an IPv6 literal bracketed as RFC 2732 wants."""
    if ":" in host:
        return "opc.tcp://[%s]:%d" % (host, port)
    return "opc.tcp://%s:%d" % (host, port)


def opcua_hello(host, port, timeout=1.5):
    """Speak the OPC-UA UACP HELLO handshake (OPC 10000-6 s7.1.2).

    A server answers ACK, or ERR if it dislikes our endpoint URL -- either way
    it is an OPC-UA server. Anything else is not. Returns
    (verdict, detail) where verdict is "opcua", "no", or "unreachable".
    """
    endpoint = (endpoint_url(host, port) + "/").encode("utf-8")
    body = struct.pack("<IIIII", 0, 65536, 65536, 0, 0)
    body += struct.pack("<i", len(endpoint)) + endpoint
    msg = b"HELF" + struct.pack("<I", 8 + len(body)) + body

    sock = None
    try:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.sendall(msg)
        head = sock.recv(8)
        if len(head) < 8:
            return "no", "closed without a reply"
        kind = head[:3]
        if kind == b"ACK":
            size = struct.unpack("<I", head[4:8])[0]
            rest = sock.recv(max(0, min(size - 8, 4096)))
            if len(rest) >= 4:
                ver = struct.unpack("<I", rest[:4])[0]
                return "opcua", "ACK, protocol version %d" % ver
            return "opcua", "ACK"
        if kind == b"ERR":
            size = struct.unpack("<I", head[4:8])[0]
            rest = sock.recv(max(0, min(size - 8, 4096)))
            code = struct.unpack("<I", rest[:4])[0] if len(rest) >= 4 else 0
            reason = ""
            if len(rest) >= 8:
                slen = struct.unpack("<i", rest[4:8])[0]
                if 0 < slen <= len(rest) - 8:
                    reason = rest[8:8 + slen].decode("utf-8", "replace")
            return "opcua", "ERR 0x%08X %s" % (code, reason)
        return "no", "replied %r" % kind
    except socket.timeout:
        return "no", "no reply within %.1fs" % timeout
    except OSError as exc:
        if exc.errno in (errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH):
            return "unreachable", os.strerror(exc.errno)
        return "unreachable", str(exc)
    finally:
        if sock is not None:
            sock.close()


def identify_server(host, port, timeout=5.0):
    """Ask a confirmed server who it is (GetEndpoints -> ApplicationName/Uri).

    Optional enrichment: needs asyncua, which elmbpsu_opcua.py already depends
    on. Without it the handshake still proves a server is there, we just
    cannot name it. Read-only -- GetEndpoints is the unauthenticated discovery
    call every OPC-UA client makes before connecting.
    """
    try:
        import asyncio
        from asyncua import Client
    except ImportError:
        return None

    async def go():
        client = Client(url=endpoint_url(host, port), timeout=timeout)
        eps = await client.connect_and_get_server_endpoints()
        out = []
        for ep in eps:
            app = ep.Server
            row = (str(app.ApplicationName.Text), str(app.ApplicationUri),
                   str(app.ProductUri), str(ep.EndpointUrl))
            if row not in out:
                out.append(row)
        return out

    try:
        return asyncio.run(go())
    except Exception as exc:                     # any protocol/timeout failure
        return [("<no answer to GetEndpoints: %s>" % type(exc).__name__, "", "", "")]


def probe_candidates(listeners, extra_ports, probe_all):
    """Which listening ports to greet. Deduplicated by (host, port)."""
    seen = set()
    out = []
    for ent in listeners:
        port = ent["port"]
        if not probe_all:
            if port not in KNOWN_OPCUA_PORTS:
                if port < PROBE_MIN_PORT:
                    continue
                if any(lo <= port <= hi for lo, hi in PROBE_SKIP_RANGES):
                    continue
        host = ent["addr"]
        if host in ("0.0.0.0", "::", "?"):
            host = "::1" if ent["v6"] else "127.0.0.1"
        key = (host, port)
        if key in seen:
            continue
        seen.add(key)
        out.append((host, port, ent))
    for port in extra_ports:
        key = ("127.0.0.1", port)
        if key not in seen:
            seen.add(key)
            out.append(("127.0.0.1", port, None))
    return out


def own_ancestry():
    """This process and every shell above it.

    Their command lines mention whatever interface you typed on the command
    line, which would otherwise show up as a bogus user of that bus.
    """
    pids = set()
    pid = os.getpid()
    for _ in range(32):
        pids.add(pid)
        stat = read_text("/proc/%d/stat" % pid)
        if ")" not in stat:
            break
        try:
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (IndexError, ValueError):
            break
        if ppid <= 1 or ppid in pids:
            break
        pid = ppid
    return pids


def iter_processes():
    """(pid, user, cmdline) for every process we can read."""
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        cmd = read_text("/proc/%d/cmdline" % pid).replace("\0", " ").strip()
        if not cmd:
            cmd = read_text("/proc/%d/comm" % pid).strip()
            if cmd:
                cmd = "[%s]" % cmd
        if not cmd:
            continue
        try:
            uid = os.stat("/proc/%d" % pid).st_uid
        except OSError:
            continue
        yield pid, uid_name(uid), cmd


def scan_processes():
    """Bucket every process into opcua / can-tool / container of interest."""
    hits = {"opcua": [], "can": [], "container": []}
    mine = own_ancestry()
    for pid, user, cmd in iter_processes():
        if pid in mine:
            continue
        low = cmd.lower()
        if any(p in low for p in OPCUA_PROC_PATTERNS):
            hits["opcua"].append((pid, user, cmd))
        elif any(p in low for p in CAN_PROC_PATTERNS):
            hits["can"].append((pid, user, cmd))
        elif any(p in low for p in CONTAINER_PATTERNS) and \
                ("can" in low or "opc" in low or "quasar" in low):
            hits["container"].append((pid, user, cmd))
    return hits


# --------------------------------------------------------------------------
# 2. CAN interfaces
# --------------------------------------------------------------------------

STAT_KEYS = ("rx_packets", "tx_packets", "rx_bytes", "tx_bytes",
             "rx_dropped", "tx_dropped", "rx_errors", "tx_errors",
             "rx_missed_errors")


def can_interfaces():
    """Every ARPHRD_CAN netdev in this namespace, with sysfs facts."""
    ifaces = {}
    try:
        names = sorted(os.listdir("/sys/class/net"))
    except OSError:
        return ifaces
    for name in names:
        base = "/sys/class/net/" + name
        if read_int(base + "/type") != ARPHRD_CAN:
            continue
        dev = os.path.realpath(base + "/device") if os.path.exists(base + "/device") else ""
        driver = ""
        drv_link = base + "/device/driver"
        if os.path.exists(drv_link):
            driver = os.path.basename(os.path.realpath(drv_link))
        ifaces[name] = {
            "name": name,
            "ifindex": read_int(base + "/ifindex"),
            "operstate": read_text(base + "/operstate").strip(),
            "address": read_text(base + "/address").strip(),
            "channel": read_int(base + "/channel"),
            "mtu": read_int(base + "/mtu"),
            "driver": driver,
            "devpath": dev,
            "adapter": usb_adapter(dev),
            "stats": {k: read_int(base + "/statistics/" + k, 0) for k in STAT_KEYS},
            # filled in later
            "up": False, "can_state": "?", "bitrate": None, "sample_point": None,
            "restart_ms": None, "berr": {}, "ctrlmode": [],
        }
    return ifaces


def usb_adapter(devpath):
    """Walk up from the netdev to the USB device, so ports on one physical
    adapter can be grouped -- yanking one box takes all its ports with it."""
    if not devpath:
        return None
    path = devpath
    for _ in range(6):
        if os.path.exists(os.path.join(path, "idVendor")):
            return {
                "path": os.path.basename(path),
                "vendor": read_text(os.path.join(path, "idVendor")).strip(),
                "product": read_text(os.path.join(path, "idProduct")).strip(),
                "manufacturer": read_text(os.path.join(path, "manufacturer")).strip(),
                "name": read_text(os.path.join(path, "product")).strip(),
                "serial": read_text(os.path.join(path, "serial")).strip(),
            }
        parent = os.path.dirname(path)
        if parent == path or parent == "/sys":
            break
        path = parent
    return None


def enrich_from_ip(ifaces):
    """Link/CAN state and bitrate. Only `ip` knows these -- sysfs does not."""
    rc, out = run(["ip", "-details", "-json", "link", "show"])
    if rc != 0 or not out.strip():
        return False
    try:
        entries = json.loads(out)
    except ValueError:
        return False
    for ent in entries:
        name = ent.get("ifname")
        if name not in ifaces:
            continue
        rec = ifaces[name]
        flags = ent.get("flags", [])
        rec["up"] = "UP" in flags
        rec["lower_up"] = "LOWER_UP" in flags
        info = (ent.get("linkinfo") or {}).get("info_data") or {}
        rec["can_state"] = info.get("state", "?")
        rec["restart_ms"] = info.get("restart_ms")
        rec["ctrlmode"] = info.get("ctrlmode") or []
        bt = info.get("bittiming") or {}
        rec["bitrate"] = bt.get("bitrate")
        rec["sample_point"] = bt.get("sample_point")
        rec["berr"] = info.get("berr_counter") or {}
    return True


def sample_traffic(ifaces, seconds):
    """Second reading of the frame counters, so we can report a live rate."""
    if seconds <= 0:
        for rec in ifaces.values():
            rec["rate"] = None
        return
    before = {n: (dict(r["stats"]), time.time()) for n, r in ifaces.items()}
    time.sleep(seconds)
    for name, rec in ifaces.items():
        base = "/sys/class/net/" + name + "/statistics/"
        now = {k: read_int(base + k, 0) for k in STAT_KEYS}
        old, t0 = before[name]
        dt = max(1e-6, time.time() - t0)
        rec["rate"] = {
            "rx": (now["rx_packets"] - old["rx_packets"]) / dt,
            "tx": (now["tx_packets"] - old["tx_packets"]) / dt,
        }
        rec["stats"] = now


# --------------------------------------------------------------------------
# 3. Who is using what
# --------------------------------------------------------------------------

RCVLISTS = ("rcvlist_all", "rcvlist_sff", "rcvlist_eff", "rcvlist_err",
            "rcvlist_fil", "rcvlist_inv")


def bound_receivers(iface_names):
    """Parse /proc/net/can/rcvlist_* -> {iface: [receiver, ...]}.

    A row here means some process holds an open CAN socket bound to that
    device. It is the one signal that does not need root and does not lie:
    the kernel only keeps the entry while the socket is open.
    """
    known = set(iface_names) | {"any"}
    result = {name: [] for name in iface_names}
    result["any"] = []
    available = False
    for listname in RCVLISTS:
        text = read_text("/proc/net/can/" + listname)
        if not text:
            continue
        available = True
        for line in text.splitlines():
            col = line.split()
            if len(col) < 4 or col[0] not in known:
                continue        # header, "(canX: no entry)", or blank
            dev = col[0]
            result.setdefault(dev, []).append({
                "list": listname,
                "can_id": col[1],
                "can_mask": col[2],
                "matches": col[-2],
                "ident": col[-1],
            })
    return result, available


def can_socket_holders(use_lsof=True):
    """Processes holding an AF_CAN socket.

    lsof labels these precisely ("protocol: CAN_RAW"). Without it -- or for
    processes we cannot read -- fall back to spotting socket inodes that
    appear in no /proc/net protocol table, which is what an AF_CAN socket
    looks like from the outside.

    Returns (holders, coverage) where coverage says how much of the process
    table we could actually inspect.
    """
    holders = []
    seen = set()
    coverage = {"method": "none", "unreadable": 0, "total": 0}

    if use_lsof:
        rc, out = run(["lsof", "-n", "-P", "-w"], timeout=40)
        if rc in (0, 1) and out.strip():
            coverage["method"] = "lsof"
            for line in out.splitlines():
                if "protocol: CAN" not in line:
                    continue
                col = line.split()
                if len(col) < 9:
                    continue
                proto = line.split("protocol:", 1)[1].strip()
                pid = int(col[1]) if col[1].isdigit() else -1
                seen.add((pid, col[3]))
                holders.append({
                    "pid": pid,
                    "user": col[2],
                    "comm": col[0],
                    "fd": col[3],
                    "proto": proto,
                    "cmd": read_text("/proc/%d/cmdline" % pid).replace("\0", " ").strip(),
                })

    # Always cross-check with the /proc scan: it is cheap, and it catches an
    # lsof too old to label AF_CAN sockets. An AF_CAN socket is one that shows
    # up in no /proc/net protocol table.
    if not holders:
        coverage["method"] = ("lsof + /proc scan" if coverage["method"] == "lsof"
                              else "/proc scan")
        known_inodes = set()
        for proto in ("tcp", "tcp6", "udp", "udp6", "udplite", "udplite6",
                      "raw", "raw6", "unix", "packet", "netlink", "sctp"):
            for line in read_text("/proc/net/" + proto).splitlines()[1:]:
                for tok in line.split():
                    if tok.isdigit() and len(tok) > 4:
                        known_inodes.add(tok)
        for pid, user, cmd in iter_processes():
            fddir = "/proc/%d/fd" % pid
            try:
                fds = os.listdir(fddir)
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(os.path.join(fddir, fd))
                except OSError:
                    continue
                m = re.match(r"socket:\[(\d+)\]$", target)
                if m and m.group(1) not in known_inodes and (pid, fd) not in seen:
                    seen.add((pid, fd))
                    holders.append({
                        "pid": pid, "user": user, "comm": cmd.split()[0][:20],
                        "fd": fd, "proto": "non-IP socket (likely CAN)",
                        "cmd": cmd,
                    })

    # How much of the process table could we see at all?
    for pid, _user, _cmd in iter_processes():
        coverage["total"] += 1
        if not os.access("/proc/%d/fd" % pid, os.R_OK):
            coverage["unreadable"] += 1
    return holders, coverage


def iface_hints_from_cmdlines(iface_names):
    """Processes whose command line names a CAN interface.

    Neither rcvlist (device, no pid) nor lsof (pid, no device) can join the
    two on its own. A command line that says `candump can11` closes the gap
    for the common cases.
    """
    hints = {}
    mine = own_ancestry()
    word = re.compile(r"(?<![A-Za-z0-9_])(%s)(?![A-Za-z0-9_])"
                      % "|".join(re.escape(n) for n in iface_names))
    for pid, user, cmd in iter_processes():
        if pid in mine:
            continue
        for match in set(word.findall(cmd)):
            hints.setdefault(match, []).append((pid, user, cmd))
    return hints


def classify(rec, receivers, hints):
    """Boil everything down to one verdict per interface."""
    nrecv = len(receivers)
    rate = rec.get("rate") or {}
    fps = (rate.get("rx", 0) or 0) + (rate.get("tx", 0) or 0)
    total = rec["stats"]["rx_packets"] + rec["stats"]["tx_packets"]
    state = rec["can_state"]

    if state == "BUS-OFF":
        return "BUS-OFF", "controller is bus-off -- wiring/termination or wrong bitrate"
    if not rec["up"]:
        if nrecv:
            return "DOWN/CLAIMED", "link is down but %d socket(s) still bound" % nrecv
        return "FREE", "link down, nothing bound -- yours to configure"
    if nrecv:
        who = hints.get(rec["name"])
        detail = "%d socket(s) bound" % nrecv
        if fps >= 0.5:
            detail += ", %s frames/s" % human_rate(fps)
        if who:
            detail += " -- pid %d (%s)" % (who[0][0], who[0][1])
        return "IN USE", detail
    if fps >= 0.5:
        return "LIVE/UNCLAIMED", ("up and carrying %s frames/s but no socket bound "
                                  "here -- another node is talking" % human_rate(fps))
    if total > 0:
        return "UP/IDLE", "up, nothing bound, silent now (%d frames historically)" % total
    return "UP/UNUSED", "up, nothing bound, no traffic ever"


FREE_VERDICTS = ("FREE", "UP/UNUSED", "UP/IDLE")


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def hr(title):
    print()
    print(title)
    print("-" * len(title))


def report_opcua(args):
    listeners = listening_tcp()
    print("%d listening TCP socket(s) on this host" % len(listeners))

    candidates = probe_candidates(listeners, args.port, args.probe_all)
    confirmed, rejected = [], []
    if args.probe:
        print("greeting %d candidate port(s) with an OPC-UA HELLO ..." % len(candidates))
        for host, port, ent in candidates:
            verdict, detail = opcua_hello(host, port, args.probe_timeout)
            row = {"host": host, "port": port, "detail": detail,
                   "user": ent["user"] if ent else "?",
                   "addr": ent["addr"] if ent else "-",
                   "note": KNOWN_OPCUA_PORTS.get(port, ""),
                   "identity": None}
            (confirmed if verdict == "opcua" else rejected).append(row)
        if args.identify:
            for row in confirmed:
                row["identity"] = identify_server(row["host"], row["port"],
                                                  args.probe_timeout * 3)
    else:
        print("(handshake probing disabled with --no-probe)")

    hr("1. OPC-UA servers")
    if not args.probe:
        occupied = [e for e in listeners if e["port"] in KNOWN_OPCUA_PORTS]
        if occupied:
            print("Without probing, all that can be said is which well-known OPC-UA")
            print("ports are occupied -- see below. Drop --no-probe to confirm them.")
        else:
            print("No well-known OPC-UA port is occupied. A server on a non-standard")
            print("port would only show up under a handshake probe -- drop --no-probe.")
    elif confirmed:
        print("%d confirmed OPC-UA server(s) -- these answered the UACP handshake:"
              % len(confirmed))
        for row in confirmed:
            print()
            print("  endpoint  %s" % endpoint_url(row["host"], row["port"]))
            print("  owner     uid of the listening socket: %s" % row["user"])
            print("  handshake %s" % row["detail"])
            if row["note"]:
                print("  port      %s" % row["note"])
            if row["identity"]:
                for name, uri, product, url in row["identity"]:
                    print("  identity  %s" % name)
                    if uri:
                        print("            application %s" % uri)
                    if product:
                        print("            product     %s" % product)
                    if url and url != endpoint_url(row["host"], row["port"]):
                        print("            advertises  %s" % url)
            elif args.identify:
                print("  identity  unknown (install asyncua to name it)")
        if args.probe:
            print()
            print("  %d other listening port(s) were greeted and are not OPC-UA."
                  % len(rejected))
    elif args.probe:
        print("No port on this host answered an OPC-UA handshake.")
        print("Nothing is serving OPC-UA right now -- port 48012 is free for your server.")
        print("(%d listening port(s) greeted and rejected.)" % len(rejected))

    procs = scan_processes()
    if procs["opcua"]:
        hr("   processes that look OPC-UA related")
        for pid, user, cmd in procs["opcua"]:
            print("  pid %-8d %-10s %s" % (pid, user, cmd[:120]))
        print()
        print("  A process here without a confirmed endpoint above is a client, a")
        print("  manager, or a server that is not listening yet -- not a conflict.")
    if procs["container"]:
        hr("   containers that may hold CAN/OPC-UA workloads")
        for pid, user, cmd in procs["container"]:
            print("  pid %-8d %-10s %s" % (pid, user, cmd[:120]))
        print()
        print("  A container in its own network namespace has its own CAN and port")
        print("  view; this script only sees the namespace it runs in.")

    interesting = [e for e in listeners if e["port"] in KNOWN_OPCUA_PORTS]
    if interesting:
        hr("   well-known OPC-UA ports that are occupied")
        for ent in interesting:
            print("  %-6d %-16s owner %-10s  %s"
                  % (ent["port"], ent["addr"], ent["user"], KNOWN_OPCUA_PORTS[ent["port"]]))
    elif 48012 not in [e["port"] for e in listeners]:
        print()
        print("  Port 48012 (CanOpenOpcUa default) is free.")

    return {"confirmed": confirmed, "rejected": rejected,
            "listeners": listeners, "processes": procs}


def report_can(args):
    ifaces = can_interfaces()

    hr("2. CAN interfaces")
    if not ifaces:
        print("No CAN interfaces exist in this network namespace.")
        print()
        print("  Check that the adapter is plugged in and its driver is loaded:")
        print("    lsusb | grep -iE 'peak|kvaser|systec|ixxat|canable'")
        print("    lsmod | grep -E '^can|peak|kvaser|systec|gs_usb'")
        print("  A SysTec USB-CANmodul needs SysTec's out-of-tree driver; PEAK,")
        print("  Kvaser and CANable/candleLight are in-tree (see README.md s2).")
        return ifaces, {}, {}, {}

    have_ip = enrich_from_ip(ifaces)
    if not have_ip:
        print("(`ip -details -json link show` unavailable: no bitrate or CAN state)")
    sample_traffic(ifaces, args.sample)

    receivers, rcv_ok = bound_receivers(list(ifaces))
    hints = iface_hints_from_cmdlines(list(ifaces))

    print("%d CAN interface(s), %s"
          % (len(ifaces),
             "traffic sampled over %.1fs" % args.sample if args.sample > 0
             else "traffic not sampled (--sample 0)"))
    print()
    print("  %-7s %-6s %-13s %-9s %-8s %-12s %s"
          % ("IFACE", "LINK", "CAN STATE", "BITRATE", "SOCKETS", "RATE f/s", "TOTAL FRAMES"))
    for name, rec in sorted(ifaces.items(), key=lambda kv: kv[1]["ifindex"] or 0):
        rate = rec.get("rate") or {}
        fps = (rate.get("rx", 0) or 0) + (rate.get("tx", 0) or 0)
        total = rec["stats"]["rx_packets"] + rec["stats"]["tx_packets"]
        print("  %-7s %-6s %-13s %-9s %-8s %-12s %s"
              % (name,
                 "UP" if rec["up"] else "DOWN",
                 rec["can_state"],
                 ("%dk" % (rec["bitrate"] // 1000)) if rec["bitrate"] else "-",
                 len(receivers.get(name, [])),
                 human_rate(fps) if rec.get("rate") is not None else "-",
                 "{:,}".format(total)))

    hr("   physical adapters behind those ports")
    groups = {}
    for name, rec in ifaces.items():
        ad = rec["adapter"]
        key = ad["path"] if ad else (rec["driver"] or "unknown")
        groups.setdefault(key, []).append((name, rec))
    for key in sorted(groups):
        members = sorted(groups[key], key=lambda kv: kv[1]["ifindex"] or 0)
        ad = members[0][1]["adapter"]
        if ad:
            label = "%s %s [%s:%s]" % (ad["manufacturer"] or "?", ad["name"] or "?",
                                       ad["vendor"], ad["product"])
            if ad["serial"]:
                label += " serial %s" % ad["serial"]
        else:
            label = "driver %s" % (members[0][1]["driver"] or "unknown")
        ports = ", ".join("%s(ch%s)" % (n, r["channel"] if r["channel"] is not None else "?")
                          for n, r in members)
        print("  %-14s %s" % (key, label))
        print("  %-14s ports: %s" % ("", ports))
    print()
    print("  Ports on one adapter are independent buses -- reconfiguring one does")
    print("  not disturb the others -- but unplugging or reloading the driver")
    print("  takes every port on that box down at once.")

    problems = []
    for name, rec in sorted(ifaces.items()):
        st = rec["stats"]
        bad = {k: st[k] for k in ("rx_errors", "tx_errors", "rx_dropped",
                                  "tx_dropped", "rx_missed_errors") if st.get(k)}
        if bad or rec["can_state"] in ("BUS-OFF", "ERROR-WARNING", "ERROR-PASSIVE"):
            problems.append((name, rec, bad))
    if problems:
        hr("   error counters worth a look")
        for name, rec, bad in problems:
            detail = ", ".join("%s=%d" % kv for kv in sorted(bad.items())) or "-"
            print("  %-7s state %-14s %s" % (name, rec["can_state"], detail))
        print()
        print("  rx_dropped/rx_missed usually means frames arrived faster than a")
        print("  socket drained them, not a wiring fault. BUS-OFF or a climbing")
        print("  error-passive count does mean wiring, termination or bitrate.")

    return ifaces, receivers, hints, {"rcvlist": rcv_ok}


def report_usage(args, ifaces, receivers, hints, opcua=None):
    hr("3. Used vs. unused")
    if not ifaces:
        print("Nothing to classify.")
        return {}

    holders, coverage = can_socket_holders(use_lsof=not args.no_lsof)
    verdicts = {}
    for name, rec in sorted(ifaces.items(), key=lambda kv: kv[1]["ifindex"] or 0):
        verdict, detail = classify(rec, receivers.get(name, []), hints)
        verdicts[name] = (verdict, detail)

    print("  %-7s %-16s %s" % ("IFACE", "VERDICT", "EVIDENCE"))
    for name, (verdict, detail) in verdicts.items():
        print("  %-7s %-16s %s" % (name, verdict, detail))

    busy = [n for n, (v, _d) in verdicts.items() if v in ("IN USE", "DOWN/CLAIMED")]
    live = [n for n, (v, _d) in verdicts.items() if v == "LIVE/UNCLAIMED"]
    free = [n for n, (v, _d) in verdicts.items() if v == "FREE"]
    idle = [n for n, (v, _d) in verdicts.items() if v in ("UP/IDLE", "UP/UNUSED")]

    hr("   processes holding CAN sockets")
    if holders:
        for h in sorted(holders, key=lambda x: x["pid"]):
            print("  pid %-8d %-10s %-12s fd %-4s %s"
                  % (h["pid"], h["user"], h["comm"][:12], h["fd"], h["proto"]))
            if h["cmd"]:
                print("  %-8s %s" % ("", h["cmd"][:110]))
    else:
        print("  None visible (%s)." % coverage["method"])
        if os.geteuid() != 0:
            print("  You can only see your own processes' file descriptors, so this")
            print("  says nothing about other users. The socket counts above come")
            print("  from the kernel and do cover everybody.")
    if hints:
        hr("   command lines naming a CAN interface")
        for name in sorted(hints):
            for pid, user, cmd in hints[name]:
                print("  %-7s pid %-8d %-10s %s" % (name, pid, user, cmd[:100]))

    print()
    print("  Attribution note: the kernel's receive list (/proc/net/can/rcvlist_*)")
    print("  names the interface but not the process; lsof names the process but")
    print("  not the interface. This report joins them only where a command line")
    print("  or a single unambiguous match allows it.")
    if coverage["unreadable"]:
        print("  Could not inspect %d of %d processes (owned by other users)%s."
              % (coverage["unreadable"], coverage["total"],
                 "" if os.geteuid() == 0 else " -- rerun with sudo for full attribution"))

    hr("Verdict")
    if busy:
        print("  IN USE, do not touch  : %s" % ", ".join(busy))
    if live:
        print("  LIVE but unclaimed    : %s" % ", ".join(live))
        print("    Frames are moving on that bus with nothing bound here. Another")
        print("    node is talking. Do not assume it is yours.")
    if idle:
        print("  UP, configured, quiet : %s" % ", ".join(idle))
        print("    Somebody brought these up and left them. Nothing is bound right")
        print("    now, but ask before reconfiguring the bitrate.")
    if free:
        print("  FREE                  : %s" % ", ".join(free))
    if not free and not idle:
        print("  No CAN interface looks free. Ask whoever owns the busy ones first.")

    if free or idle:
        pick = (free or idle)[0]
        rec = ifaces[pick]
        print()
        print("  For the ELMB PSU control bus (125 kbit/s; scan reports the node id,")
        print("  which is 63 only on a factory-default crate):")
        print()
        if rec["up"] and rec["bitrate"] == 125000:
            print("    elmbpsu-can --iface %s scan" % pick)
        else:
            print("    sudo ip link set %s down" % pick)
            print("    sudo ip link set %s type can bitrate 125000" % pick)
            print("    sudo ip link set %s up" % pick)
            print("    elmbpsu-can --iface %s scan" % pick)
        print()
        print("  Then set Bus/@port in config/config-elmbpsu.xml to \"%s\"." % pick)
    if busy:
        print()
        print("  Do not run elmbpsu-can against %s: a second CANopen master on a"
              % busy[0])
        print("  bus that already has one will collide on SDO transfers and can")
        print("  switch branches out from under the other operator.")

        canopen = []
        for row in (opcua or {}).get("confirmed", []):
            for name, uri, product, _url in (row.get("identity") or []):
                if "canopen" in (uri + product + name).lower():
                    canopen.append((row, name))
        if canopen:
            row, name = canopen[0]
            print()
            print("  Note: a CanOpenOpcUa server is already running here --")
            print("    %s" % name)
            print("    at %s" % endpoint_url(row["host"], row["port"]))
            print("  It holds its SocketCAN port open for as long as it runs, which")
            print("  makes it the most likely owner of %s. Confirm with whoever"
                  % ", ".join(busy))
            print("  started it before bringing up a bus of your own.")

    return {"verdicts": verdicts, "holders": holders, "coverage": coverage,
            "busy": busy, "live": live, "free": free, "idle": idle}


def report_namespaces():
    rc, out = run(["ip", "netns", "list"])
    names = [l.split()[0] for l in out.splitlines() if l.strip()] if rc == 0 else []
    if names:
        hr("Network namespaces")
        print("  This host has %d named namespace(s): %s" % (len(names), ", ".join(names)))
        print("  CAN devices and listening ports inside them are invisible from here.")
        print("  Inspect one with: sudo ip netns exec <name> ip -details link show")


def main():
    ap = argparse.ArgumentParser(
        description="Diagnose CAN interfaces and OPC-UA servers before an ELMB PSU test.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Reports, in order: OPC-UA servers running here; the CAN interfaces\n"
               "that exist and the hardware behind them; which are in use and which\n"
               "are free. Read-only -- it never configures or transmits on a bus.")
    ap.add_argument("--sample", type=float, default=2.0, metavar="SEC",
                    help="seconds to sample frame counters for a live rate (default 2, 0 to skip)")
    ap.add_argument("--port", type=int, action="append", default=[], metavar="N",
                    help="also probe this TCP port for OPC-UA (repeatable)")
    ap.add_argument("--probe-all", action="store_true",
                    help="send an OPC-UA HELLO to every listening port, not just plausible ones")
    ap.add_argument("--no-probe", dest="probe", action="store_false",
                    help="do not connect to anything; classify ports by number alone")
    ap.add_argument("--probe-timeout", type=float, default=1.5, metavar="SEC",
                    help="per-port handshake timeout (default 1.5)")
    ap.add_argument("--no-identify", dest="identify", action="store_false",
                    help="do not ask confirmed servers for their name (skips asyncua)")
    ap.add_argument("--no-lsof", action="store_true",
                    help="skip lsof and use the /proc socket scan instead")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON instead of the report")
    args = ap.parse_args()

    if args.json:
        listeners = listening_tcp()
        candidates = probe_candidates(listeners, args.port, args.probe_all)
        confirmed = []
        if args.probe:
            for host, port, ent in candidates:
                verdict, detail = opcua_hello(host, port, args.probe_timeout)
                if verdict == "opcua":
                    confirmed.append({
                        "host": host, "port": port, "detail": detail,
                        "endpoint": endpoint_url(host, port),
                        "user": ent["user"] if ent else "?",
                        "identity": identify_server(host, port, args.probe_timeout * 3)
                        if args.identify else None,
                    })
        ifaces = can_interfaces()
        enrich_from_ip(ifaces)
        sample_traffic(ifaces, args.sample)
        receivers, _ok = bound_receivers(list(ifaces))
        hints = iface_hints_from_cmdlines(list(ifaces))
        holders, coverage = can_socket_holders(use_lsof=not args.no_lsof)
        out = {
            "opcua": {"confirmed": confirmed,
                      "processes": scan_processes()["opcua"],
                      "listening_ports": listeners},
            "can": {name: {k: v for k, v in rec.items() if k != "adapter"}
                    for name, rec in ifaces.items()},
            "adapters": {name: rec["adapter"] for name, rec in ifaces.items()},
            "receivers": receivers,
            "holders": holders,
            "coverage": coverage,
            "verdicts": {name: classify(rec, receivers.get(name, []), hints)
                         for name, rec in ifaces.items()},
        }
        json.dump(out, sys.stdout, indent=2, default=str)
        print()
        return 0

    print("CAN / OPC-UA environment diagnostics")
    print("host %s   user %s   %s"
          % (socket.gethostname(), pwd.getpwuid(os.geteuid()).pw_name,
             time.strftime("%Y-%m-%d %H:%M:%S")))

    opcua = report_opcua(args)
    ifaces, receivers, hints, _meta = report_can(args)
    report_usage(args, ifaces, receivers, hints, opcua)
    report_namespaces()
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
