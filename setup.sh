#!/usr/bin/env bash
#
# Restore the ELMB PSU working environment on a fresh machine.
#
#   git clone https://github.com/jimmat92/can_psu_test.git
#   cd can_psu_test
#   ./setup.sh
#
# Fetches the three reference repositories that the tools in this repo were
# reverse-engineered from, pins them to the exact commits that were read, writes
# the .gitignore, and verifies that every source file cited in HANDOFF.md is
# present afterwards.
#
# The reference repos are READ-ONLY documentation for us: nothing here imports
# or executes them at runtime. They are pinned because HANDOFF.md cites specific
# files, and an upstream change could silently invalidate a citation.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$SCRIPT_DIR"
PIN=1
SUBMODULES=0
DO_PIP=0
PROTO="https"

# repo | https url path | pinned commit | human-readable version
REPOS=(
  "CanOpenOpcUa|atlas-dcs-opcua-servers/CanOpenOpcUa|a34bbabfeae8b501150b0d8f47728b8539914d09|v1.0.0"
  "fwElmb|atlas-dcs-fwcomponents/fwElmb|094ecdd25ad5a3f19a1e37f4fa9415992e1bb426|9.4.6-15-g094ecdd"
  "fwElmbPSU|atlas-dcs-fwcomponents/fwElmbPSU|101665b1983da85d56c65fb449fb22f298ca2468|9.2.3"
)

# files HANDOFF.md cites - if one of these is missing the workspace is broken
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
                   Faster and smaller, but HANDOFF.md citations may drift.
  --submodules     also init CanOpenOpcUa's submodules (CanModuleMain, LogIt).
                   Required only if you intend to BUILD the OPC-UA server.
  --pip            pip install asyncua (needed by elmbpsu_opcua.py only).
  --check          verify an existing workspace, clone nothing.
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
    --pip)        DO_PIP=1; shift ;;
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

# Optional trees, not required by anything here (see HANDOFF.md section 8).
/fwInstallation-*/
/jcop-framework-*/

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

# ---------------------------------------------------------------- verify
verify() {
  local rc=0
  info "Verifying reference sources cited by HANDOFF.md"
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

  info "Verifying the toolchain"
  if command -v python3 >/dev/null; then
    ok "python3 $(python3 -c 'import platform;print(platform.python_version())')"
  else
    fail "python3 not found"; rc=1
  fi
  if python3 -c 'import socket; raise SystemExit(0 if hasattr(socket,"AF_CAN") else 1)' 2>/dev/null; then
    ok "socket.AF_CAN available (elmbpsu_can.py needs nothing else)"
  else
    fail "socket.AF_CAN missing - elmbpsu_can.py needs Linux SocketCAN support"; rc=1
  fi
  if python3 -c 'import asyncua' 2>/dev/null; then
    ok "asyncua $(python3 -c 'import asyncua;print(asyncua.__version__)')"
  else
    warn "asyncua not installed - elmbpsu_opcua.py will not run (./setup.sh --pip)"
  fi
  for t in ip candump; do
    if command -v "$t" >/dev/null; then ok "$t"; else warn "$t not found"; fi
  done
  if ls /sys/class/net | grep -q '^can\|^vcan'; then
    ok "CAN interface present: $(ls /sys/class/net | grep '^can\|^vcan' | tr '\n' ' ')"
  else
    warn "no CAN interface - see HANDOFF.md section 6 before testing hardware"
  fi

  info "Verifying our own tools"
  if python3 "$SCRIPT_DIR/selftest.py" >/dev/null 2>&1; then
    ok "selftest.py passes"
  else
    fail "selftest.py FAILED - run it directly to see why"; rc=1
  fi
  if python3 -c "import xml.dom.minidom as m; m.parse('$SCRIPT_DIR/config-elmbpsu.xml')" 2>/dev/null; then
    ok "config-elmbpsu.xml is well-formed"
  else
    fail "config-elmbpsu.xml is not well-formed"; rc=1
  fi
  return $rc
}

# ---------------------------------------------------------------- main
echo
info "ELMB PSU workspace setup"
echo "    repo      : $SCRIPT_DIR"
echo "    reference : $DEST"
echo

if [[ $CHECK_ONLY -eq 0 ]]; then
  write_gitignore
  for entry in "${REPOS[@]}"; do
    IFS='|' read -r name path commit version <<< "$entry"
    clone_one "$name" "$path" "$commit" "$version"
  done
  if [[ $DO_PIP -eq 1 ]]; then
    info "Installing asyncua"
    python3 -m pip install --quiet asyncua && ok "asyncua installed"
  fi
  echo
fi

if verify; then
  echo
  info "Workspace ready. Next: QUICKSTART.md"
  exit 0
else
  echo
  fail "Workspace incomplete - see the FAIL lines above"
  exit 1
fi
