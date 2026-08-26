#!/usr/bin/env bash
#
# Restore the ELMB PSU working environment on a fresh machine.
#
#   git clone https://github.com/jimmat92/can_psu_test.git
#   cd can_psu_test
#   ./setup.sh
#   source .venv/bin/activate
#
# Does three things:
#   1. fetches the three reference repositories that the tools here were
#      reverse-engineered from, pinned to the exact commits that were read;
#   2. builds a .venv that knows where everything lives - asyncua installed,
#      lib/ importable, and the three tools on PATH as elmbpsu-can,
#      elmbpsu-opcua and can-diag;
#   3. verifies that every source file cited in docs/REFERENCE.md is present
#      and that our own tools still pass their offline self-test.
#
# The reference repos are READ-ONLY documentation for us: nothing here imports
# or executes them at runtime. They are pinned because docs/REFERENCE.md cites
# specific files, and an upstream change could silently invalidate a citation.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$SCRIPT_DIR"
VENV="$SCRIPT_DIR/.venv"
PIN=1
SUBMODULES=0
DO_VENV=1
PROTO="https"
PYTHON=""

# repo | https url path | pinned commit | human-readable version
REPOS=(
  "CanOpenOpcUa|atlas-dcs-opcua-servers/CanOpenOpcUa|a34bbabfeae8b501150b0d8f47728b8539914d09|v1.0.0"
  "fwElmb|atlas-dcs-fwcomponents/fwElmb|094ecdd25ad5a3f19a1e37f4fa9415992e1bb426|9.4.6-15-g094ecdd"
  "fwElmbPSU|atlas-dcs-fwcomponents/fwElmbPSU|101665b1983da85d56c65fb449fb22f298ca2468|9.2.3"
)

# files docs/REFERENCE.md cites - if one is missing the workspace is broken
REQUIRED_FILES=(
  "fwElmbPSU/scripts/libs/fwElmbPSU/fwElmbPSU.ctl"
  "fwElmbPSU/scripts/libs/fwElmbPSU/fwElmbPSUConstants.ctl"
  "fwElmbPSU/scripts/fwElmbPSU/fwElmbPSU.postInstall"
  "fwElmbPSU/source/ElmbPsuIntroduction.pdf"
  "fwElmb/scripts/libs/fwElmb/fwElmbUser.ctl"
  "fwElmb/config/fwElmb/OPCUA_nodeType_ELMB.xml"
  "CanOpenOpcUa/Design/Design.xml"
  "CanOpenOpcUa/bin/CANopen_def_STDELMB_DO_RPDO.xmle"
  "CanOpenOpcUa/bin/ServerConfig.xml"
)

# our own files, relative to the repo root
OUR_FILES=(
  "tests/can_diag.py"
  "lib/elmbpsu_can.py"
  "lib/elmbpsu_opcua.py"
  "tests/selftest.py"
  "config/config-elmbpsu.xml"
  "config/ServerConfig-elmbpsu.xml"
)

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '  \033[1;32mok\033[0m   %s\n' "$*"; }
warn()  { printf '  \033[1;33mwarn\033[0m %s\n' "$*"; }
fail()  { printf '  \033[1;31mFAIL\033[0m %s\n' "$*"; }

usage() {
  cat <<'USAGE'
Usage: ./setup.sh [options]

  --dest DIR       where to place the reference repos (default: this repo's
                   directory). Use "--dest .." to match a layout where they
                   are siblings of this repo.
  --ssh            clone over SSH (git@gitlab.cern.ch:...) instead of HTTPS.
                   Use this if you have an SSH key registered with CERN GitLab.
  --latest         clone the tip of master instead of the pinned commits.
                   Faster and smaller, but doc citations may drift.
  --submodules     also init CanOpenOpcUa's submodules (CanModuleMain, LogIt).
                   Required only if you intend to BUILD the OPC-UA server.
  --no-venv        skip creating .venv. Only elmbpsu_opcua.py needs it;
                   tests/can_diag.py and lib/elmbpsu_can.py are stdlib only.
  --python PATH    interpreter to build the venv with (default: the newest
                   python3.X found on PATH).
  --check          verify an existing workspace, clone nothing, build nothing.
  -h, --help       this message

The repositories live on gitlab.cern.ch and may require CERN credentials.
For HTTPS, a personal access token works as the password. If a clone fails
with an authentication error, that is why - it is not a problem with this
script.
USAGE
}

CHECK_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest)       DEST="$2"; shift 2 ;;
    --ssh)        PROTO="ssh"; shift ;;
    --latest)     PIN=0; shift ;;
    --submodules) SUBMODULES=1; shift ;;
    --no-venv)    DO_VENV=0; shift ;;
    --python)     PYTHON="$2"; shift 2 ;;
    --pip)        shift ;;   # accepted for compatibility; the venv implies it
    --check)      CHECK_ONLY=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

command -v git >/dev/null || { echo "error: git is not installed" >&2; exit 1; }
mkdir -p "$DEST"
DEST="$(cd "$DEST" && pwd)"

# ---------------------------------------------------------------- gitignore
write_gitignore() {
  info "Writing .gitignore"
  cat > "$SCRIPT_DIR/.gitignore" <<'IGNORE'
# --- reference repositories fetched by setup.sh -----------------------------
# Read-only sources that the tools here were reverse-engineered from.
# Never commit them: they are large, and they have their own upstreams.
/CanOpenOpcUa/
/fwElmb/
/fwElmbPSU/

# --- python ----------------------------------------------------------------
__pycache__/
*.py[cod]
.venv/
venv/
*.egg-info/

# --- local runtime output --------------------------------------------------
*.log
candump*.txt

# --- editors / OS ----------------------------------------------------------
.vscode/
.idea/
*.swp
*~
.DS_Store
IGNORE
  ok ".gitignore written"
}

# ---------------------------------------------------------------- clone
clone_one() {
  local name="$1" path="$2" commit="$3" version="$4"
  local target="$DEST/$name"

  if [[ -d "$target/.git" ]]; then
    ok "$name already present at $target"
    return 0
  fi
  if [[ -d "$DEST/../$name/.git" ]]; then
    warn "$name not in $DEST but found in $(cd "$DEST/.." && pwd)/$name - skipping"
    return 0
  fi

  local url
  if [[ "$PROTO" == "ssh" ]]; then
    url="git@gitlab.cern.ch:${path}.git"
  else
    url="https://gitlab.cern.ch/${path}.git"
  fi

  info "Cloning $name ($version) from $url"
  if [[ $PIN -eq 1 ]]; then
    # full history needed so the pinned commit is reachable
    git clone --quiet "$url" "$target"
    git -C "$target" checkout --quiet "$commit"
    ok "$name pinned at ${commit:0:12} ($version)"
  else
    git clone --quiet --depth 1 "$url" "$target"
    ok "$name at tip of master (NOT pinned)"
  fi

  if [[ "$name" == "CanOpenOpcUa" && $SUBMODULES -eq 1 ]]; then
    info "Initialising CanOpenOpcUa submodules"
    git -C "$target" submodule update --init --recursive --quiet
    ok "submodules initialised"
  fi
}

# ---------------------------------------------------------------- venv
pick_python() {
  if [[ -n "$PYTHON" ]]; then echo "$PYTHON"; return; fi
  local c
  for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$c" >/dev/null 2>&1 \
       && "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,6) else 1)' 2>/dev/null \
       && "$c" -c 'import venv' 2>/dev/null; then
      command -v "$c"; return
    fi
  done
}

make_venv() {
  local py
  py="$(pick_python)"
  if [[ -z "$py" ]]; then
    fail "no python3 with the venv module found - install python3-venv, or pass --no-venv"
    return 1
  fi

  if [[ -x "$VENV/bin/python" ]]; then
    ok "venv already present at $VENV ($("$VENV/bin/python" -V 2>&1))"
  else
    info "Creating venv at $VENV with $py ($("$py" -V 2>&1))"
    "$py" -m venv "$VENV"
    ok "venv created"
  fi

  info "Installing asyncua into the venv"
  if "$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1; then :; fi
  if "$VENV/bin/python" -m pip install --quiet asyncua; then
    ok "asyncua $("$VENV/bin/python" -c 'import asyncua;print(asyncua.__version__)')"
  else
    warn "pip install asyncua failed (no network?) - elmbpsu_opcua.py will not run"
  fi

  # --- make lib/ importable from inside the venv, from any directory --------
  local sp
  sp="$("$VENV/bin/python" -c 'import sysconfig;print(sysconfig.get_paths()["purelib"])')"
  printf '%s\n' "$SCRIPT_DIR/lib" > "$sp/can_psu_test.pth"
  ok "lib/ on the venv import path (import elmbpsu_can works anywhere)"

  # --- console entry points -------------------------------------------------
  wrapper() {   # wrapper <name> <script-relative-to-repo>
    cat > "$VENV/bin/$1" <<WRAP
#!/usr/bin/env bash
# generated by setup.sh - do not edit, re-run ./setup.sh instead
exec "$VENV/bin/python" "$SCRIPT_DIR/$2" "\$@"
WRAP
    chmod +x "$VENV/bin/$1"
  }
  wrapper elmbpsu-can   lib/elmbpsu_can.py
  wrapper elmbpsu-opcua lib/elmbpsu_opcua.py
  wrapper can-diag      tests/can_diag.py
  wrapper elmbpsu-selftest tests/selftest.py
  ok "elmbpsu-can, elmbpsu-opcua, can-diag, elmbpsu-selftest on PATH once activated"

  # --- repo paths as environment variables ---------------------------------
  if ! grep -q '# >>> can_psu_test >>>' "$VENV/bin/activate" 2>/dev/null; then
    cat >> "$VENV/bin/activate" <<ENVBLOCK

# >>> can_psu_test >>>
# added by setup.sh - repo paths, so commands do not depend on your cwd
export CAN_PSU_TEST="$SCRIPT_DIR"
export CAN_PSU_CONFIG="$SCRIPT_DIR/config"
# <<< can_psu_test <<<
ENVBLOCK
  fi
  ok "CAN_PSU_TEST and CAN_PSU_CONFIG exported on activate"
}

# ---------------------------------------------------------------- verify
verify() {
  local rc=0
  info "Verifying reference sources cited by docs/REFERENCE.md"
  for f in "${REQUIRED_FILES[@]}"; do
    if [[ -f "$DEST/$f" ]]; then
      ok "$f"
    elif [[ -f "$DEST/../$f" ]]; then
      ok "$f (in parent directory)"
    else
      fail "$f  MISSING"
      rc=1
    fi
  done

  info "Verifying the repo layout"
  for f in "${OUR_FILES[@]}"; do
    if [[ -f "$SCRIPT_DIR/$f" ]]; then ok "$f"; else fail "$f  MISSING"; rc=1; fi
  done

  info "Verifying the toolchain"
  if command -v python3 >/dev/null; then
    ok "python3 $(python3 -c 'import platform;print(platform.python_version())')"
  else
    fail "python3 not found"; rc=1
  fi
  if python3 -c 'import socket; raise SystemExit(0 if hasattr(socket,"AF_CAN") else 1)' 2>/dev/null; then
    ok "socket.AF_CAN available (lib/elmbpsu_can.py needs nothing else)"
  else
    fail "socket.AF_CAN missing - lib/elmbpsu_can.py needs Linux SocketCAN support"; rc=1
  fi
  if [[ -x "$VENV/bin/python" ]]; then
    if "$VENV/bin/python" -c 'import asyncua' 2>/dev/null; then
      ok "venv asyncua $("$VENV/bin/python" -c 'import asyncua;print(asyncua.__version__)')"
    else
      warn "venv exists but asyncua is not installed - re-run ./setup.sh"
    fi
    if "$VENV/bin/python" -c 'import elmbpsu_can' 2>/dev/null; then
      ok "venv can import elmbpsu_can from lib/"
    else
      warn "venv cannot import elmbpsu_can - re-run ./setup.sh to rewrite the .pth"
    fi
  elif [[ $DO_VENV -eq 1 ]]; then
    warn "no venv at $VENV - run ./setup.sh without --no-venv"
  fi
  for t in ip candump; do
    if command -v "$t" >/dev/null; then ok "$t"; else warn "$t not found"; fi
  done
  if ls /sys/class/net | grep -q '^can\|^vcan'; then
    ok "CAN interface present: $(ls /sys/class/net | grep '^can\|^vcan' | tr '\n' ' ')"
    info "Which of them are free: ./tests/can_diag.py"
  else
    warn "no CAN interface - see docs/HANDOFF.md before testing hardware"
  fi

  info "Verifying our own tools"
  # selftest.py imports elmbpsu_can by name; the venv .pth is what makes that
  # resolve, so it has to run under the venv interpreter.
  if [[ -x "$VENV/bin/python" ]]; then
    if "$VENV/bin/python" "$SCRIPT_DIR/tests/selftest.py" >/dev/null 2>&1; then
      ok "tests/selftest.py passes"
    else
      fail "tests/selftest.py FAILED - run elmbpsu-selftest to see why"; rc=1
    fi
  else
    warn "tests/selftest.py skipped - it needs the venv (drop --no-venv)"
  fi
  for x in config/config-elmbpsu.xml config/ServerConfig-elmbpsu.xml; do
    if python3 -c "import xml.dom.minidom as m; m.parse('$SCRIPT_DIR/$x')" 2>/dev/null; then
      ok "$x is well-formed"
    else
      fail "$x is not well-formed"; rc=1
    fi
  done
  for s in tests/can_diag.py lib/elmbpsu_can.py lib/elmbpsu_opcua.py; do
    if python3 -c "import ast; ast.parse(open('$SCRIPT_DIR/$s').read())" 2>/dev/null; then
      ok "$s parses"
    else
      fail "$s does not parse"; rc=1
    fi
  done
  return $rc
}

# ---------------------------------------------------------------- main
echo
info "ELMB PSU workspace setup"
echo "    repo      : $SCRIPT_DIR"
echo "    reference : $DEST"
echo "    venv      : $([[ $DO_VENV -eq 1 ]] && echo "$VENV" || echo "(skipped)")"
echo

if [[ $CHECK_ONLY -eq 0 ]]; then
  write_gitignore
  for entry in "${REPOS[@]}"; do
    IFS='|' read -r name path commit version <<< "$entry"
    clone_one "$name" "$path" "$commit" "$version"
  done
  if [[ $DO_VENV -eq 1 ]]; then
    make_venv || true
  fi
  echo
fi

if verify; then
  echo
  info "Workspace ready."
  [[ $DO_VENV -eq 1 ]] && echo "    source .venv/bin/activate"
  echo "    Next: docs/QUICKSTART.md"
  exit 0
else
  echo
  fail "Workspace incomplete - see the FAIL lines above"
  exit 1
fi
