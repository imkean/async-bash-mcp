#!/usr/bin/env python3
"""Example: drive async-bash-mcp over stdio.

Shows the shape of a real client conversation: spawn, poll until finished,
then clean up. Run it with ``uv run python examples/demo.py``.
"""

import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def demo_async_bash_mcp():
    print("Starting async-bash-mcp demo...")

    server_params = StdioServerParameters(command="uv", args=["run", "async-bash-mcp"])

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("Connected")

            tools = await session.list_tools()
            print(f"Tools: {[tool.name for tool in tools.tools]}")

            # A short command: spawn, then poll for the output.
            spawned = await session.call_tool("spawn", {"command": "echo hello"})
            process_id = spawned.structured_content["id"]
            print(f"Spawned {process_id}")

            polled = await session.call_tool(
                "poll", {"process_id": process_id, "wait": 5000}
            )
            print(f"stdout: {polled.structured_content['stdout'].strip()}")
            print(f"exit code: {polled.structured_content['exitCode']}")

            # A long-running command: poll repeatedly for incremental output,
            # then terminate it rather than leaving it running.
            long_running = await session.call_tool(
                "spawn",
                {"command": "for i in 1 2 3; do echo step $i; sleep 1; done"},
            )
            long_id = long_running.structured_content["id"]
            print(f"Spawned {long_id}")

            for _ in range(4):
                polled = await session.call_tool(
                    "poll", {"process_id": long_id, "wait": 1000}
                )
                data = polled.structured_content
                if data["stdout"]:
                    print(f"  {data['stdout'].strip()}")
                if data["finished"]:
                    print(f"  finished, exit code {data['exitCode']}")
                    break

            # Working directory per call.
            cwd_spawned = await session.call_tool(
                "spawn", {"command": "pwd", "cwd": "/tmp"}
            )
            cwd_id = cwd_spawned.structured_content["id"]
            polled = await session.call_tool(
                "poll", {"process_id": cwd_id, "wait": 5000}
            )
            print(f"cwd: {polled.structured_content['stdout'].strip()}")

    print("Done")


if __name__ == "__main__":
    asyncio.run(demo_async_bash_mcp())
