# Running async-bash-mcp as a shared HTTP server

Tested on WSL2 / Ubuntu with a `systemd --user` session. One server process
serves every opencode instance; clients connect over streamable HTTP.

## Install / upgrade

```bash
git -C ~/codes/async-bash-mcp pull --ff-only
cd ~/codes/async-bash-mcp && uv sync        # rebuilds .venv if deps changed
systemctl --user restart async-bash-mcp.service
```

First-time setup:

```bash
cd ~/codes/async-bash-mcp && uv sync
mkdir -p ~/.config/systemd/user
cp deploy/async-bash-mcp.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now async-bash-mcp.service
```

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
- Process managers are keyed by MCP client id, and the streamable HTTP
  transport reports `client_id == None`, so **all clients share one process
  namespace**. Sessions do not get per-client cleanup; abandoned processes are
  handled by `ASYNC_BASH_IDLE_TIMEOUT` instead.
- A server restart drops in-flight results. Ids are seeded from the clock so a
  stale id fails as "not found" instead of resolving to a new process.
