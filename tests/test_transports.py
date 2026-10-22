"""End-to-end tests over the real transports.

The rest of the suite drives tool functions with hand-written context doubles,
which is exactly how ``ctx.client_id`` -- always ``None`` for real clients --
went unnoticed. These tests talk to an actual server over stdio and over
streamable HTTP, so a broken per-session identity fails them.
"""

import socket
import sys
import threading
import time

import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

import async_bash_mcp as abm


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def http_server():
    """Serve the real app once for the module, the way deploy/serve.sh does."""
    port = _free_port()
    config = uvicorn.Config(
        abm.mcp.streamable_http_app(),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "streamable HTTP server did not start"

    yield f"http://127.0.0.1:{port}/mcp"

    server.should_exit = True
    thread.join(timeout=15)


async def _spawn(session, command: str) -> int:
    result = await session.call_tool("spawn", {"command": command})
    assert not result.is_error, result
    return result.structured_content["id"]


async def _poll(session, process_id: int, wait: int = 5000, terminate: bool = False):
    return await session.call_tool(
        "poll",
        {"process_id": process_id, "wait": wait, "terminate": terminate},
    )


@pytest.mark.asyncio
async def test_stdio_transport():
    """The `uvx async-bash-mcp` default path."""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "async_bash_mcp"]
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            assert [tool.name for tool in tools.tools] == [
                "spawn",
                "list_processes",
                "poll",
            ]

            process_id = await _spawn(session, "echo hello-from-stdio")
            polled = await _poll(session, process_id)

            assert not polled.is_error, polled
            assert polled.structured_content["stdout"] == "hello-from-stdio\n"
            assert polled.structured_content["finished"] is True


@pytest.mark.asyncio
async def test_http_transport_round_trip(http_server):
    """spawn then poll must land on the same manager across two requests."""
    async with streamable_http_client(http_server) as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()

            process_id = await _spawn(session, "echo hello-from-http")
            polled = await _poll(session, process_id)

            assert not polled.is_error, polled
            assert polled.structured_content["stdout"] == "hello-from-http\n"
            assert polled.structured_content["finished"] is True

            listed = await session.call_tool("list_processes", {})
            assert listed.structured_content["processes"] == []


@pytest.mark.asyncio
async def test_http_sessions_are_isolated(http_server):
    async with streamable_http_client(http_server) as (read_a, write_a, *_):
        async with ClientSession(read_a, write_a) as session_a:
            await session_a.initialize()
            process_id = await _spawn(session_a, "sleep 30")

            try:
                async with streamable_http_client(http_server) as (
                    read_b,
                    write_b,
                    *_,
                ):
                    async with ClientSession(read_b, write_b) as session_b:
                        await session_b.initialize()

                        listed = await session_b.call_tool("list_processes", {})
                        assert listed.structured_content["processes"] == []

                        polled = await _poll(session_b, process_id, wait=100)
                        assert polled.is_error, "session B saw session A's process"
            finally:
                await _poll(session_a, process_id, wait=5000, terminate=True)
