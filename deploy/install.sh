#!/usr/bin/env bash
# Render and install the systemd --user unit for this checkout.
#
# The tracked unit is a template (deploy/async-bash-mcp.service.in) with @REPO@
# in place of an absolute path, so the checkout can live anywhere and the
# committed file does not encode one machine's layout. This script substitutes
# the path this repo actually has and writes the real unit.
#
#   deploy/install.sh             render + daemon-reload
#   deploy/install.sh --now      ... and enable + start
#   deploy/install.sh --restart  ... and restart (wedges connected clients!)
#
# Re-run it after moving or renaming the checkout, and after editing the
# template: the installed unit is a rendered copy, not a symlink.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$REPO/deploy/async-bash-mcp.service.in"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/async-bash-mcp.service"
ACTION=install

usage() {
  cat >&2 <<'EOF'
usage: deploy/install.sh [--now | --restart]

  (no flag)   render the unit and run systemctl --user daemon-reload
  --now       also enable and start it (safe on a first install)
  --restart   also restart it -- this wedges connected MCP clients, so only
              run it when no session is active
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --now) ACTION=now ;;
    --restart) ACTION=restart ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
  shift
done

if [[ ! -f "$TEMPLATE" ]]; then
  echo "missing template: $TEMPLATE" >&2
  exit 1
fi

mkdir -p "$UNIT_DIR"

# Escape the characters sed treats specially on the replacement side.
escaped="${REPO//\\/\\\\}"
escaped="${escaped//&/\\&}"
escaped="${escaped//|/\\|}"

if [[ -L "$UNIT" ]]; then
  echo "replacing symlink $UNIT with a rendered copy" >&2
fi

sed "s|@REPO@|$escaped|g" "$TEMPLATE" >"$UNIT.tmp"
mv "$UNIT.tmp" "$UNIT"
echo "installed $UNIT -> $REPO"

if ! command -v systemctl >/dev/null 2>&1 ||
  ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "no systemd --user session; unit written, load it when one is available" >&2
  exit 0
fi

systemctl --user daemon-reload

case "$ACTION" in
  now) systemctl --user enable --now async-bash-mcp.service ;;
  restart)
    echo "restarting -- connected MCP clients will need a new session" >&2
    systemctl --user restart async-bash-mcp.service
    ;;
esac
