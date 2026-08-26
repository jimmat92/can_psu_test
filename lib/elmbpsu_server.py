#!/usr/bin/env python3
"""
Start/stop wrapper for the CanOpenOpcUa server, so a Python test script can
own the server's lifecycle instead of a human running sudo in another
terminal (docs/QUICKSTART.md step 3).

The two config flags are mandatory (config/README.md) and default to the
files CAN_PSU_CONFIG points at (exported by "source .venv/bin/activate"):

    --config_file          $CAN_PSU_CONFIG/config-elmbpsu.xml
    --opcua_backend_config $CAN_PSU_CONFIG/ServerConfig-elmbpsu.xml

The server needs CAP_NET_ADMIN to (re)configure the SocketCAN link, so this
runs it under sudo by default -- pass --no-sudo if you built a private copy
with "sudo setcap cap_net_admin+ep" (see docs/QUICKSTART.md step 3). Because
sudo is in front of it, we don't track the Popen child's pid (that's sudo's
pid, and signalling it is not reliable across sudo versions) -- instead we
find the real server pid with "pgrep -f", the same way a human would check
"is it already running" before starting a second one, and signal that pid
with sudo kill.
"""

import argparse
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_BINARY = "/opt/labTempMonitor/bin/CanOpenOpcUa"


def _default_config_dir():
    env = os.environ.get("CAN_PSU_CONFIG")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "config"


class ServerError(Exception):
    pass


class OpcUaServer:
    """Handle to one CanOpenOpcUa instance, identified by its --config_file."""

    def __init__(self, config_file=None, opcua_backend_config=None,
                 binary=DEFAULT_BINARY, extra_args=(), use_sudo=True,
                 log_file="server.log"):
        cfg_dir = _default_config_dir()
        self.binary = str(binary)
        self.config_file = str(config_file or cfg_dir / "config-elmbpsu.xml")
        self.opcua_backend_config = str(
            opcua_backend_config or cfg_dir / "ServerConfig-elmbpsu.xml")
        self.extra_args = list(extra_args)
        self.use_sudo = use_sudo
        self.log_file = str(log_file) if log_file else None
        self._proc = None  # the sudo (or direct) Popen handle, for cleanup only

    # ---- process discovery -------------------------------------------
    def _sudo(self, cmd):
        return (["sudo"] + cmd) if self.use_sudo else cmd

    def pid(self):
        """Real server pid, found by matching our --config_file, or None.

        Anchored with ^ so this matches only a process whose command line
        *starts* with the binary path -- i.e. the actual CanOpenOpcUa
        process sudo execve()'d, not the "sudo /opt/.../CanOpenOpcUa ..."
        wrapper process, which also contains the binary path and config
        file as substrings and would otherwise match too."""
        try:
            out = subprocess.check_output(
                ["pgrep", "-f", f"^{re.escape(self.binary)}.*"
                 f"{re.escape(self.config_file)}"],
                text=True, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            return None
        pids = [int(p) for p in out.split()]
        return pids[0] if pids else None

    def is_running(self):
        return self.pid() is not None

    # ---- lifecycle -----------------------------------------------------
    def start(self, wait=True, timeout=15.0):
        """Launch the server. Returns its pid. Raises ServerError if it was
        already running or never showed up in the process table."""
        existing = self.pid()
        if existing is not None:
            raise ServerError(f"CanOpenOpcUa already running for "
                               f"{self.config_file} (pid {existing})")

        cmd = self._sudo([self.binary,
                          "--config_file", self.config_file,
                          "--opcua_backend_config", self.opcua_backend_config,
                          *self.extra_args])
        log_fh = open(self.log_file, "ab") if self.log_file else subprocess.DEVNULL
        # Deliberately NOT isolated into its own process group: sudo needs to
        # read the password from the controlling terminal, and it can only do
        # that from the terminal's *foreground* process group. Putting the
        # child in a separate group (e.g. preexec_fn=os.setpgrp) makes the
        # kernel SIGTTIN it the moment it tries to read -- it just hangs,
        # suspended, and the prompt sits there unable to accept input. The
        # tradeoff: Ctrl-C on this script's terminal also reaches the child
        # directly (normal Unix job-control behaviour for anything sharing a
        # foreground pgrp), so it can shut itself down before stop() runs.
        # That still satisfies "the server is terminated when the script
        # finishes" -- it just happens via the child's own SIGINT handler
        # instead of our SIGTERM. wait_ready() below is what actually fixes
        # the "looks hung" symptom that provoked the Ctrl-C in the first
        # place.
        self._proc = subprocess.Popen(cmd, stdout=log_fh,
                                      stderr=subprocess.STDOUT, stdin=None)
        if log_fh not in (subprocess.DEVNULL,):
            log_fh.close()  # child inherited its own fd; we don't need ours

        if not wait:
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pid = self.pid()
            if pid is not None:
                return pid
            if self._proc.poll() is not None and self._proc.returncode != 0:
                raise ServerError(
                    f"CanOpenOpcUa exited immediately (code {self._proc.returncode})"
                    f" -- check {self.log_file or 'its stderr'}")
            time.sleep(0.2)
        raise ServerError(f"CanOpenOpcUa did not appear in the process table "
                           f"within {timeout}s -- check {self.log_file or 'its stderr'}")

    def stop(self, timeout=10.0, force=True):
        """SIGTERM the real server pid (docs/QUICKSTART.md: 'plain SIGTERM
        shuts it down cleanly'), escalating to SIGKILL after timeout if
        force=True. Returns True if it was running and is now stopped."""
        pid = self.pid()
        if pid is None:
            return False
        subprocess.run(self._sudo(["kill", "-TERM", str(pid)]), check=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.pid() is None:
                return True
            time.sleep(0.2)
        if force:
            subprocess.run(self._sudo(["kill", "-KILL", str(pid)]), check=True)
            return True
        return False

    def restart(self, timeout=15.0):
        self.stop()
        return self.start(timeout=timeout)

    def wait_ready(self, timeout=15.0, marker="Opened endpoint"):
        """Block until the log shows the OPC-UA endpoint has opened, or the
        server has died trying. This is the reliable way to know the server
        is ready: ServerConfig's <Url> is opc.tcp://[NodeName]:48012, and
        [NodeName] is substituted with the *hostname* (confirmed in
        server.log: "Opened endpoint: opc.tcp://<host>:48012"), which does
        not necessarily resolve to 127.0.0.1 -- TCP-probing loopback can
        spin for the full timeout and never connect even though the server
        is up and healthy. Requires log_file (the default)."""
        if not self.log_file:
            raise ServerError("wait_ready() needs a log_file to poll (start() "
                               "with log_file set, the default)")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise ServerError(f"server process exited (code "
                                   f"{self._proc.returncode}) before its "
                                   f"endpoint opened -- see {self.log_file}")
            try:
                with open(self.log_file, "r", errors="replace") as fh:
                    if marker in fh.read():
                        return True
            except FileNotFoundError:
                pass
            time.sleep(0.2)
        return False

    def wait_for_port(self, host=None, port=48012, timeout=15.0):
        """Block until something accepts TCP connections on host:port. Only
        useful if you know the endpoint is reachable at `host` -- it binds
        to socket.gethostname(), not necessarily 127.0.0.1 (see
        wait_ready(), which doesn't need to guess this and is the default)."""
        if host is None:
            host = socket.gethostname()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=1.0):
                    return True
            except OSError:
                time.sleep(0.2)
        return False

    # ---- context manager ------------------------------------------------
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False


def main():
    p = argparse.ArgumentParser(
        description="Start/stop the CanOpenOpcUa server used for ELMB PSU testing.")
    p.add_argument("--binary", default=DEFAULT_BINARY)
    p.add_argument("--config-file", default=None,
                   help="default: $CAN_PSU_CONFIG/config-elmbpsu.xml")
    p.add_argument("--opcua-backend-config", default=None,
                   help="default: $CAN_PSU_CONFIG/ServerConfig-elmbpsu.xml")
    p.add_argument("--no-sudo", action="store_true",
                   help="use if the binary already has CAP_NET_ADMIN (setcap)")
    p.add_argument("--log-file", default="server.log")
    p.add_argument("--verbose-server", action="store_true",
                   help="add --lSdo INF --lNodeMgmt INF --print_cobids_tables")
    p.add_argument("--port", type=int, default=48012,
                   help="OPC-UA port to poll for 'status'/'start --wait-port'")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="launch the server")
    s.add_argument("--no-wait", action="store_true",
                   help="don't wait for the pid to appear before returning")
    s.add_argument("--wait-ready", action="store_true",
                   help="also wait (via server.log) for the OPC-UA endpoint to open")
    s.add_argument("--timeout", type=float, default=15.0)

    s = sub.add_parser("stop", help="stop the server")
    s.add_argument("--timeout", type=float, default=10.0)
    s.add_argument("--no-force", action="store_true",
                   help="don't SIGKILL if it ignores SIGTERM")

    s = sub.add_parser("restart", help="stop, then start")
    s.add_argument("--timeout", type=float, default=15.0)

    sub.add_parser("status", help="is it running, and is the port open")

    args = p.parse_args()

    extra_args = []
    if args.verbose_server:
        extra_args += ["--lSdo", "INF", "--lNodeMgmt", "INF", "--print_cobids_tables"]

    server = OpcUaServer(config_file=args.config_file,
                         opcua_backend_config=args.opcua_backend_config,
                         binary=args.binary, extra_args=extra_args,
                         use_sudo=not args.no_sudo, log_file=args.log_file)

    try:
        if args.cmd == "start":
            pid = server.start(wait=not args.no_wait, timeout=args.timeout)
            print(f"started, pid {pid}" if pid else "started (not waited for)")
            if args.wait_ready:
                ok = server.wait_ready(timeout=args.timeout)
                print("OPC-UA endpoint open" if ok else "OPC-UA endpoint never opened")
                return 0 if ok else 1
            return 0
        if args.cmd == "stop":
            stopped = server.stop(timeout=args.timeout, force=not args.no_force)
            print("stopped" if stopped else "was not running")
            return 0
        if args.cmd == "restart":
            pid = server.restart(timeout=args.timeout)
            print(f"restarted, pid {pid}")
            return 0
        if args.cmd == "status":
            pid = server.pid()
            if pid is None:
                print("not running")
                return 1
            port_open = server.wait_for_port(port=args.port, timeout=0.5)
            print(f"running, pid {pid}, OPC-UA port {args.port} "
                  f"{'open' if port_open else 'closed'}")
            return 0
    except ServerError as exc:
        sys.exit(f"error: {exc}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
