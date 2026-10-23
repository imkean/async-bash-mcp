# Running async-bash-mcp as a shared HTTP server

Tested on WSL2 / Ubuntu with a `systemd --user` session. One server process
serves every opencode instance; clients connect over streamable HTTP.

## Install / upgrade

The tracked unit is a **template**: `deploy/async-bash-mcp.service.in` has
`@REPO@` where an absolute path would go, and `deploy/install.sh` substitutes
this checkout's real path. That keeps the committed file free of any one
machine's layout, and lets the checkout live wherever you like.

First-time setup, run from the checkout:

```bash
uv sync                     # creates .venv from the pinned dependencies
deploy/install.sh --now     # render the unit, enable, start
```

Upgrade:

```bash
git pull --ff-only
uv sync                     # only rebuilds .venv if dependencies changed
deploy/install.sh --restart # re-render, then restart (wedges live clients)
```

Re-run `deploy/install.sh` after moving or renaming the checkout, and after
editing the template: the installed unit is a rendered copy, not a symlink.

If you would rather do it by hand, `deploy/install.sh` is only a `sed` of
`@REPO@` plus `systemctl --user daemon-reload`.

`opencode.jsonc`:

```jsonc
"async-bash": {
  "type": "remote",
  "url": "http://127.0.0.1:8769/mcp",
  "oauth": false
}
```

## Checks

```bash
systemctl --user status async-bash-mcp.service
journalctl --user -u async-bash-mcp.service -n 20
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8769/mcp   # 400/406 = alive
```

Note that `systemctl` reports cgroup memory including page cache from every
command the service ran; use `ps -o rss= -p $(systemctl --user show -p MainPID --value async-bash-mcp.service)`
for the real figure.

## Environment knobs

| Variable | Default | Meaning |
|---|---|---|
| `ASYNC_BASH_SHELL` | `$SHELL` | shell used for `<shell> -c <command>` |
| `ASYNC_BASH_DEFAULT_CWD` | server cwd | cwd for `spawn(cwd=None)` |
| `ASYNC_BASH_MAX_OUTPUT_CHARS` | `1048576` | per-stream cap on unpolled output |
| `ASYNC_BASH_IDLE_TIMEOUT` | `1800` | kill processes not polled for this long (0 = off) |
| `ASYNC_BASH_FINISHED_RETENTION` | `3600` | forget finished bookkeeping after this long |
| `ASYNC_BASH_REAP_INTERVAL` | `60` | reaper period |
| `ASYNC_BASH_HOST` / `ASYNC_BASH_PORT` | `127.0.0.1` / `8769` | listen address for HTTP transports |

## Known limits

- **Restarting the service wedges connected clients.** opencode keeps its MCP
  session id after the restart and cannot recover (`400 No valid session ID`);
  the affected session has to be restarted. Restart only when no session is
  active.
- No authentication. The port is bound to loopback; anything running as this
  user can drive the server, which means arbitrary command execution.
- **Processes are scoped to the transport session.** Each streamable HTTP
  `mcp-session-id` gets its own process namespace, so two opencode sessions
  cannot see or poll each other's processes. When a session disconnects its
  processes are terminated immediately (the server pushes cleanup onto the
  SDK's per-connection teardown stack), so a vanished editor no longer leaves
  commands running; `ASYNC_BASH_IDLE_TIMEOUT` stays as the backstop for clients
  that disappear without a clean disconnect.
- A server restart drops in-flight results. Ids are seeded from the clock so a
  stale id fails as "not found" instead of resolving to a new process.
