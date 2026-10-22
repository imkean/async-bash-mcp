#!/usr/bin/env bash
# Run async-bash-mcp as one long-lived MCP server over streamable HTTP.
#
# Why: opencode opens one stdio MCP connection per working directory. When it
# reaps one of those transports the server's stdin hits EOF, the process exits
# and any in-flight tool call dies ("MCP server process exited with code 0").
# A single HTTP instance serves every client and survives transport churn.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "no venv at $REPO/.venv — run: cd $REPO && uv sync" >&2
  exit 1
fi

exec "$PY" -m async_bash_mcp \
  --transport streamable-http \
  --host "${HOST:-127.0.0.1}" \
  --port "${PORT:-8769}"
