"""
async-bash-mcp: spawn shell commands asynchronously and poll them.

Local fork of https://github.com/xhuw/async-bash-mcp (MIT). Differences from
upstream are marked with "[fork]" and cover:

* HTTP/SSE transport with ``--host`` / ``--port`` (lets one long-lived server
  serve every client instead of one stdio process per working directory)
* bounded output buffers with O(1) appends (upstream copied the whole buffer
  per 1KiB chunk under a global lock)
* process-group termination, an idle-process reaper and stale bookkeeping
  pruning (upstream leaked processes and entries when a client vanished)
* configurable shell and default working directory
* process ids that do not collide with ids from a previous server run
* runs on MCP Python SDK 2.x, where ``FastMCP`` is ``MCPServer`` and
  ``Context.client_id`` no longer exists (per-client state is keyed on the
  transport session instead -- see ``_client_key``)
"""

import asyncio
import logging
import logging.handlers
import os
import re
import signal
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

from platformdirs import user_log_dir
from pydantic import BaseModel

from mcp.server.mcpserver import MCPServer, Context
from mcp.server.session import ServerSession


# --- configuration -----------------------------------------------------------
# Every knob is read from the environment once at import time; the constants are
# looked up from the module at call time so tests can monkeypatch them.


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# [fork] Shell used for ``shell -c <command>``. Upstream used ``$SHELL``, which
# under systemd can be an interactive-only shell (zsh does not word-split
# unquoted variables, so bash-style commands silently misbehave).
SHELL = os.environ.get("ASYNC_BASH_SHELL") or os.environ.get("SHELL") or "/bin/bash"

# [fork] Working directory for ``spawn(cwd=None)``. Upstream inherited the
# server's cwd, which is ``$HOME`` for a systemd service.
DEFAULT_CWD = os.environ.get("ASYNC_BASH_DEFAULT_CWD") or None

# [fork] Cap on unpolled output per stream, in characters.
MAX_OUTPUT_CHARS = _env_int("ASYNC_BASH_MAX_OUTPUT_CHARS", 1024 * 1024)

# [fork] Terminate processes nobody polled for this many seconds (0 disables).
IDLE_TIMEOUT = _env_int("ASYNC_BASH_IDLE_TIMEOUT", 1800)

# [fork] Forget bookkeeping for finished processes after this many seconds.
FINISHED_RETENTION = _env_int("ASYNC_BASH_FINISHED_RETENTION", 3600)

# [fork] How often the reaper runs.
REAP_INTERVAL = _env_int("ASYNC_BASH_REAP_INTERVAL", 60)


def str_last_n_lines(string: str, n: int) -> Optional[str]:
    """Return the last ``n`` lines of ``string`` (None if there are fewer)."""
    idx = len(string)
    for _ in range(n):
        idx = string.rfind("\n", 0, idx)
        if idx < 0:
            return None
    return string[idx + 1 :]


class OutputBuffer:
    """Bounded text buffer with cheap appends.

    ``poll`` drains the buffer, so in steady state it only holds output the
    client has not seen yet. When a chatty process outruns the client the
    oldest characters are dropped and counted, so the caller can tell the
    agent that output was lost instead of growing without bound.
    """

    __slots__ = ("_chunks", "_size", "_limit", "dropped")

    def __init__(self, limit: Optional[int] = None) -> None:
        self._chunks: deque[str] = deque()
        self._size = 0
        self._limit = MAX_OUTPUT_CHARS if limit is None else limit
        self.dropped = 0

    def __len__(self) -> int:
        return self._size

    def append(self, text: str) -> None:
        if not text:
            return
        self._chunks.append(text)
        self._size += len(text)
        while self._size > self._limit and self._chunks:
            excess = self._size - self._limit
            head = self._chunks[0]
            if len(head) <= excess:
                self._chunks.popleft()
                self._size -= len(head)
                self.dropped += len(head)
            else:
                self._chunks[0] = head[excess:]
                self._size -= excess
                self.dropped += excess

    def take(self) -> str:
        """Drain the buffer, returning everything not consumed yet."""
        if not self._chunks:
            return ""
        text = "".join(self._chunks)
        self._chunks.clear()
        self._size = 0
        return text

    def tail(self, lines: int = 5) -> str:
        """Return the last ``lines`` lines without draining the buffer."""
        text = ""
        for chunk in reversed(self._chunks):
            text = chunk + text
            if text.count("\n") > lines:
                break
        return str_last_n_lines(text, lines) or text


@dataclass
class ProcessInfo:
    id: int
    command: str
    process: asyncio.subprocess.Process
    start_time: float
    cwd: Optional[str]
    stdout: OutputBuffer = field(default_factory=OutputBuffer)
    stderr: OutputBuffer = field(default_factory=OutputBuffer)
    last_poll: float = 0.0
    accessed: bool = False
    finished: bool = False
    exit_code: Optional[int] = None


class ProcessManager:
    # Global counter for unique process IDs across all sessions. [fork] Seeded
    # from the clock: after a restart, ids from the previous run cannot silently
    # resolve to a brand new process (upstream always restarted at 1).
    _global_next_id = 1 + int(time.time()) % 100_000_000
    _global_id_lock = threading.Lock()

    def __init__(self):
        self._processes: Dict[int, ProcessInfo] = {}
        # [fork] Spawns that have not registered a process yet. Without this the
        # reaper can drop an "empty" manager while a spawn is between getting
        # the manager and inserting into ``_processes``, stranding the process.
        self._pending = 0
        self._lock = threading.Lock()

    @classmethod
    def _get_next_global_id(cls) -> int:
        with cls._global_id_lock:
            current_id = cls._global_next_id
            cls._global_next_id += 1
            return current_id

    def _get_next_id(self) -> int:
        return self._get_next_global_id()

    def _validate_command(self, command: str) -> None:
        """Validate command for security issues"""
        if not command or not command.strip():
            raise ValueError("Command cannot be empty")

        # Basic length check to prevent extremely long commands
        if len(command) > 10000:
            raise ValueError("Command too long (max 10000 characters)")

        # Check for potentially dangerous patterns
        dangerous_patterns = [
            r"rm\s+-rf\s+/",  # rm -rf /
            r":\(\)\{\s*:\|:\s*&\s*\};:",  # fork bomb
            r"dd\s+if=/dev/zero",  # disk filling
        ]

        for pattern in dangerous_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                raise ValueError(
                    f"Command contains potentially dangerous pattern: {pattern}"
                )

    def _validate_cwd(self, cwd: Optional[str]) -> Optional[str]:
        """Validate and normalize working directory path"""
        if cwd is None:
            # [fork] Apply the configured default instead of inheriting the
            # server's own cwd.
            cwd = DEFAULT_CWD
        if cwd is None:
            return None

        if not cwd.strip():
            raise ValueError("Working directory cannot be empty string")

        try:
            # Resolve and validate path
            path = Path(cwd).resolve()

            # Check if path exists and is a directory
            if not path.exists():
                raise ValueError(f"Working directory does not exist: {cwd}")
            if not path.is_dir():
                raise ValueError(f"Working directory is not a directory: {cwd}")

            # Convert back to string
            return str(path)
        except (OSError, ValueError) as e:
            raise ValueError(f"Invalid working directory: {e}")

    async def spawn_process(self, command: str, cwd: Optional[str] = None) -> int:
        # [fork] Mark the manager busy before the first await, so the reaper
        # cannot decide it is empty while this spawn is in flight.
        with self._lock:
            self._pending += 1
        try:
            # Validate inputs
            self._validate_command(command)
            validated_cwd = self._validate_cwd(cwd)

            shell = SHELL

            # Use the user's shell to execute the command. [fork] The child gets
            # its own session/process group so terminate() can take down the
            # whole tree (upstream killed only the direct shell child and
            # orphaned the rest).
            proc = await asyncio.create_subprocess_exec(
                shell,
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=validated_cwd,
                start_new_session=True,
            )

            process_id = self._get_next_id()
            process_info = ProcessInfo(
                id=process_id,
                command=command,
                process=proc,
                start_time=time.time(),
                cwd=validated_cwd,
                last_poll=time.time(),
            )

            with self._lock:
                self._processes[process_id] = process_info
                # Start background task to read output while holding lock
                asyncio.create_task(self._read_process_output(process_info))

            return process_id
        finally:
            with self._lock:
                self._pending -= 1

    async def _read_process_output(self, process_info: ProcessInfo):
        async def read_stream(stream, buffer: OutputBuffer):
            while True:
                try:
                    data = await stream.read(1024)
                    if not data:
                        break

                    with self._lock:
                        buffer.append(data.decode("utf-8", errors="replace"))
                except Exception:
                    break

        # Read stdout and stderr concurrently
        await asyncio.gather(
            read_stream(process_info.process.stdout, process_info.stdout),
            read_stream(process_info.process.stderr, process_info.stderr),
            return_exceptions=True,
        )

        # Wait for process to complete
        exit_code = await process_info.process.wait()

        with self._lock:
            process_info.finished = True
            process_info.exit_code = exit_code

    @staticmethod
    def _signal_process(process_info: ProcessInfo, sig: int) -> None:
        """Signal the child's whole process group, falling back to the child."""
        proc = process_info.process
        if proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), sig)
            return
        except (ProcessLookupError, PermissionError):
            pass
        try:
            if sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()
        except ProcessLookupError:
            pass

    async def terminate_process(self, process_info: ProcessInfo) -> None:
        """Terminate a process tree and wait for the shell to be reaped."""
        if process_info.finished:
            return
        self._signal_process(process_info, signal.SIGTERM)
        try:
            await asyncio.wait_for(process_info.process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            self._signal_process(process_info, signal.SIGKILL)
            await process_info.process.wait()

        with self._lock:
            process_info.finished = True
            if process_info.exit_code is None:
                process_info.exit_code = process_info.process.returncode

    def list_processes(self) -> List[Dict[str, Union[int, str, bool]]]:
        with self._lock:
            # Clean up accessed processes - create a copy to avoid modification during iteration
            processes_copy = dict(self._processes)
            to_remove = [pid for pid, proc in processes_copy.items() if proc.accessed]
            for pid in to_remove:
                if pid in self._processes:  # Double-check it still exists
                    del self._processes[pid]

            return [
                {"ID": proc.id, "command": proc.command, "done": proc.finished}
                for proc in self._processes.values()
            ]

    def is_empty(self) -> bool:
        """[fork] True when no process is tracked and none is being spawned."""
        with self._lock:
            return not self._processes and not self._pending

    async def reap(self) -> None:
        """Kill processes nobody is polling and prune stale bookkeeping. [fork]

        A detached client (closed editor, dropped HTTP session) never polls
        again, so its long-running commands used to live until the server was
        restarted. Polling is cheap and the tool contract requires it, so
        "not polled for IDLE_TIMEOUT" is a reliable liveness signal.
        """
        now = time.time()
        idle = IDLE_TIMEOUT
        retention = FINISHED_RETENTION

        with self._lock:
            stale = [
                info
                for info in self._processes.values()
                if info.finished and now - info.last_poll > retention
            ]
            for info in stale:
                self._processes.pop(info.id, None)

            candidates = [
                info
                for info in self._processes.values()
                if not info.finished and idle > 0 and now - info.last_poll > idle
            ]

        for info in candidates:
            info.stderr.append(
                f"[async-bash: terminated after {int(now - info.last_poll)}s "
                f"without a poll]\n"
            )
            logging.getLogger("async_bash_mcp").warning(
                "reaping idle process %s (%s)", info.id, info.command[:80]
            )
            await self.terminate_process(info)

    async def _emit_progress(self, process_id, progress_callback, elapsed):
        with self._lock:
            proc = self._processes.get(process_id)
            if not proc:
                return
            stdout = proc.stdout.tail(5)
            stderr = proc.stderr.tail(5)
            message = f"# `$ {proc.command}` \n\n## stdout\n\n```\n{stdout}\n```\n\n## stderr\n\n```\n{stderr}\n```\n"
        await progress_callback(round(1000 * elapsed), message)

    async def _poll_progress(self, process_id, progress_callback):
        elapsed = 0
        period = 0.3
        while True:
            try:
                await self._emit_progress(process_id, progress_callback, elapsed)
                await asyncio.sleep(period)
            except asyncio.CancelledError:
                return
            elapsed += period

    async def poll_process(
        self,
        process_id: int,
        wait_ms: int = 0,
        terminate: bool = False,
        progress_callback=None,
    ) -> Dict[str, Union[str, float, bool, int]]:
        if progress_callback is None:
            # Default no-op progress callback
            async def default_progress_callback(elapsed_ms, message):
                pass

            progress_callback = default_progress_callback

        with self._lock:
            if process_id not in self._processes:
                raise ValueError(f"Process {process_id} not found")

            process_info = self._processes[process_id]
            process_info.last_poll = time.time()

        # Terminate process if requested
        if terminate and not process_info.finished:
            await self.terminate_process(process_info)

        # Wait for specified time or process completion
        if wait_ms > 0 and not process_info.finished:
            progress = asyncio.create_task(
                self._poll_progress(process_id, progress_callback)
            )
            try:
                await asyncio.wait_for(
                    self._wait_for_finish(process_info), timeout=wait_ms / 1000.0
                )
            except asyncio.TimeoutError:
                pass
            finally:
                progress.cancel()
                await progress
                await self._emit_progress(process_id, progress_callback, wait_ms)

        with self._lock:
            # [fork] Draining the buffers keeps memory bounded; anything dropped
            # because of the cap is announced so the agent knows it lost output.
            new_stdout = process_info.stdout.take()
            new_stderr = process_info.stderr.take()
            if process_info.stdout.dropped:
                new_stdout = (
                    f"[async-bash: {process_info.stdout.dropped} chars of stdout "
                    f"discarded, buffer limit {MAX_OUTPUT_CHARS}]\n" + new_stdout
                )
                process_info.stdout.dropped = 0
            if process_info.stderr.dropped:
                new_stderr = (
                    f"[async-bash: {process_info.stderr.dropped} chars of stderr "
                    f"discarded, buffer limit {MAX_OUTPUT_CHARS}]\n" + new_stderr
                )
                process_info.stderr.dropped = 0

            process_info.last_poll = time.time()
            elapsed_time = (time.time() - process_info.start_time) * 1000

            result = {
                "stdout": new_stdout,
                "stderr": new_stderr,
                "elapsedTime": elapsed_time,
                "finished": process_info.finished,
            }

            if process_info.finished:
                # Mark as accessed
                process_info.accessed = True
                result["exitCode"] = process_info.exit_code

            return result

    async def _wait_for_finish(self, process_info: ProcessInfo):
        while not process_info.finished:
            await asyncio.sleep(0.1)


# Pydantic models for structured output
class SpawnResult(BaseModel):
    id: int


class ProcessListItem(BaseModel):
    ID: int
    command: str
    done: bool


class ListResult(BaseModel):
    processes: List[ProcessListItem]


class PollResult(BaseModel):
    stdout: str
    stderr: str
    elapsedTime: float
    finished: bool
    exitCode: Optional[int] = None


# Client-based process manager storage
client_managers: Dict[str, ProcessManager] = {}
client_lock = threading.Lock()


def _client_key(ctx) -> str:
    """Identify the caller's connection, for scoping processes. [fork]

    v1 read ``Context.client_id``, which only ever echoed a non-standard
    ``client_id`` key from the request's ``_meta`` -- nothing in the SDK or the
    spec populates it, so every real client shared one manager. v2 removed the
    attribute entirely.

    Key on what the transport actually tells us instead:

    * Streamable HTTP: the ``mcp-session-id`` header, stable across requests.
    * Legacy SSE: the ``session_id`` query parameter on the POST endpoint.
    * stdio: one client per process, so a constant is correct.
    * Anything else (stateless HTTP, in-process clients): also the constant,
      matching v1's effective behaviour of one shared manager.

    A ``client_id`` attribute is still honoured before falling back to the
    stdio constant, which keeps the test doubles and v1-style callers working.
    """
    request_context = getattr(ctx, "request_context", None)
    request = getattr(request_context, "request", None)
    if request is not None:
        headers = getattr(request, "headers", None) or {}
        session_id = headers.get("mcp-session-id")
        if session_id:
            return f"http:{session_id}"
        query = getattr(request, "query_params", None) or {}
        session_id = query.get("session_id")
        if session_id:
            return f"sse:{session_id}"

    client_id = getattr(ctx, "client_id", None)
    if client_id:
        return str(client_id)
    return "stdio"


def _drop_client_manager_if_empty(client_id: str, pm: ProcessManager) -> None:
    """Forget an idle manager whose processes are all gone. [fork]

    Keying on the transport session means a new manager per HTTP session, so
    without this the dict would grow for the life of the process.
    """
    with client_lock:
        if client_managers.get(client_id) is not pm:
            return
        if not pm.is_empty():
            return
        del client_managers[client_id]


def get_client_process_manager(client_id: str) -> ProcessManager:
    """Get or create a ProcessManager for a specific client"""
    with client_lock:
        if client_id not in client_managers:
            client_managers[client_id] = ProcessManager()
        return client_managers[client_id]


def cleanup_client_manager(client_id: str):
    """Clean up ProcessManager when client disconnects"""
    with client_lock:
        if client_id in client_managers:
            pm = client_managers[client_id]
            # Terminate all processes for this client
            with pm._lock:
                for process_info in pm._processes.values():
                    if not process_info.finished:
                        try:
                            pm._signal_process(process_info, signal.SIGTERM)
                        except ProcessLookupError:
                            pass  # Process already terminated
            del client_managers[client_id]


async def _terminate_session_processes(client_id: str) -> None:
    """Kill the processes of a session whose connection just closed. [fork]

    Registered on the connection's teardown stack, so a client that disconnects
    (or an editor that vanishes) does not leave commands running until
    ``ASYNC_BASH_IDLE_TIMEOUT``. This terminates rather than just signalling, so
    a command that ignores SIGTERM still gets SIGKILL.
    """
    with client_lock:
        pm = client_managers.get(client_id)
    if pm is None:
        return

    with pm._lock:
        pending = [info for info in pm._processes.values() if not info.finished]

    for info in pending:
        try:
            await pm.terminate_process(info)
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "session cleanup failed for process %s of %s", info.id, client_id
            )

    # Nothing can poll this session again, so drop its bookkeeping entirely
    # rather than leaving finished entries behind for the reaper to age out.
    with pm._lock:
        pm._processes.clear()

    _drop_client_manager_if_empty(client_id, pm)


def _register_session_cleanup(ctx, client_id: str) -> None:
    """Ask the SDK to run ``_terminate_session_processes`` on connection close.

    SDK 2.x unwinds ``Connection.exit_stack`` when a connection tears down and
    documents that handlers may push cleanup onto it; ``Connection.state`` is the
    public per-connection dict, which keeps this to one registration.

    ``ServerSession._connection`` is private. If a future SDK moves it, the
    ``getattr`` yields None and we fall back to the idle reaper rather than
    breaking tool calls.
    """
    connection = getattr(getattr(ctx, "session", None), "_connection", None)
    if connection is None:  # pragma: no cover - defensive, see docstring
        return
    try:
        if connection.state.get("async_bash_cleanup_registered"):
            return
        connection.state["async_bash_cleanup_registered"] = True
        connection.exit_stack.push_async_callback(
            _terminate_session_processes, client_id
        )
    except AttributeError:  # pragma: no cover - defensive, see docstring
        return


async def _reaper_loop() -> None:
    """[fork] Periodic background reaper for every known client."""
    logger = logging.getLogger("async_bash_mcp")
    while True:
        await asyncio.sleep(max(REAP_INTERVAL, 1))
        with client_lock:
            managers = list(client_managers.items())
        for client_id, pm in managers:
            try:
                await pm.reap()
            except Exception:  # pragma: no cover - defensive
                logger.exception("reaper failed for client %s", client_id)
            _drop_client_manager_if_empty(client_id, pm)


@asynccontextmanager
async def lifespan(app):
    reaper = asyncio.create_task(_reaper_loop())
    try:
        yield {"client_managers": client_managers}
    finally:
        reaper.cancel()
        try:
            await reaper
        except asyncio.CancelledError:
            pass


# Create the MCP server (FastMCP before SDK 2.x)
mcp = MCPServer("async-bash-mcp", lifespan=lifespan)
root_log = logging.getLogger()
log_dir = Path(user_log_dir("async_bash_mcp", ""))
log_dir.mkdir(parents=True, exist_ok=True)
handler = logging.handlers.RotatingFileHandler(
    log_dir / "log.log", maxBytes=5 * 1024 * 1024, backupCount=2
)
root_log.addHandler(handler)
logger = logging.getLogger("async_bash_mcp")


@mcp.tool()
async def spawn(
    command: str, cwd: Optional[str] = None, ctx: Context[ServerSession, dict] = None
) -> SpawnResult:
    """
    Launch a bash command asynchronously in a subshell.

    Returns a unique process ID that can be used to check progress with the poll tool. **ALWAYS POLL THE PROCESS AFTER SPAWNING**.

    Multiple commands can be spawned in parallel and independently polled. If the task requires running independent bash commands, run them in parrallel.

    Example:

        1. User requests the results of `test a` and `test b`
        2. spawn `test a`
        3. spawn `test b`
        4. while test a is running: poll test a
        5. while test b is running: poll test b

    Args:
        command: The bash command to execute
        cwd: Optional working directory path (defaults to current directory)

    Returns:
        SpawnResult with the unique process ID
    """

    meta = ctx.request_context.meta
    progress_token = meta.get("progress_token") if meta else None
    client_id = _client_key(ctx)
    # Register before spawning: a process that starts and then loses its client
    # mid-call must still be cleaned up.
    _register_session_cleanup(ctx, client_id)
    logger.info(
        f"Spawning process for client {client_id} with command: {command}, cwd: {cwd}, progress token: {progress_token}"
    )
    progress_message = (cwd or "") + "$ " + command
    await ctx.report_progress(0, message=progress_message)
    pm = get_client_process_manager(client_id)
    process_id = await pm.spawn_process(command, cwd)
    await ctx.report_progress(1, message=progress_message)
    return SpawnResult(id=process_id)


@mcp.tool()
async def list_processes(ctx: Context[ServerSession, dict] = None) -> ListResult:
    """
    List all currently running or recently finished processes. Processes are removed from this list once their results have been accessed via the poll tool.

    Returns:
        ListResult containing list of processes with their ID, command, and completion status
    """
    pm = get_client_process_manager(_client_key(ctx))
    processes = pm.list_processes()
    return ListResult(processes=[ProcessListItem(**proc) for proc in processes])


@mcp.tool()
async def poll(
    process_id: int,
    wait: int,
    terminate: bool = False,
    ctx: Context[ServerSession, dict] = None,
) -> PollResult:
    """
    Check the progress of a spawned process. Returns stdout/stderr output produced since the last poll call. Can optionally wait for completion or terminate the process.

    **NEVER LEAVE A PROCESS RUNNING UNPOLLLED**. Always poll the process after spawning it to ensure resources are cleaned up.

    **TERMINATE THE PROCESS IF YOU NO LONGER NEED IT**. If you don't poll the process, it will continue running indefinitely.

    Args:
        process_id: The process ID returned by spawn
        wait: Maximum milliseconds to wait for process completion. Must be greater that 0
        terminate: If True, terminate the process with SIGTERM before returning results

    Returns:
        PollResult with incremental stdout/stderr, elapsed time, and finished flag
    """
    if wait <= 0:
        raise ValueError("Wait time must be greater than 0 milliseconds")

    async def progress_callback(elapsed_ms, message):
        try:
            await ctx.report_progress(progress=elapsed_ms, total=wait, message=message)
        except Exception:
            # Connection closed or other error, ignore
            pass

    pm = get_client_process_manager(_client_key(ctx))
    try:
        result = await pm.poll_process(process_id, wait, terminate, progress_callback)
        return PollResult(**result)
    except ValueError as e:
        raise ValueError(str(e))


def main():
    """Entry point for the MCP server."""
    import argparse

    parser = argparse.ArgumentParser(description="Async Bash MCP Server")
    parser.add_argument(
        "--transport", default="stdio", choices=["stdio", "sse", "streamable-http"]
    )
    # [fork] HTTP transports need an address; upstream had no way to set one,
    # which forced people to wrap the module to serve it as a shared server.
    parser.add_argument(
        "--host", default=os.environ.get("ASYNC_BASH_HOST", "127.0.0.1")
    )
    parser.add_argument("--port", type=int, default=_env_int("ASYNC_BASH_PORT", 8769))
    args = parser.parse_args()

    # [fork] Transport options are arguments of run()/the app methods since
    # SDK 2.x; v1 set them on ``mcp.settings``, which no longer exists.
    run_kwargs = {}
    if args.transport in ("streamable-http", "sse"):
        run_kwargs = {"host": args.host, "port": args.port}

    mcp.run(transport=args.transport, **run_kwargs)


if __name__ == "__main__":
    main()
