AGENTS.md

Build / lint / test

- Install dev deps: uv sync --extra dev
- Run full tests: uv run pytest
- Run a single test: uv run pytest tests/test_async_bash_mcp.py::test_spawn
- Lint (ruff): uv run ruff check

Code style & conventions

- Use Python 3.13+ features conservatively; maintain clear, simple code.
- Formatting: follow ruff/PEP 8. Use black-style line lengths ~88. Keep imports grouped: stdlib, third-party, local.
- Imports: always use absolute imports from package root (e.g. from async_bash_mcp import spawn).
- Types: annotate public functions and methods with type hints. Prefer built-in typing (list/dict) per py3.13.
- Naming: snake_case for functions and variables, PascalCase for classes, UPPER_SNAKE for constants.
- Errors: raise specific exceptions. Validate inputs early and return plain serializable types for MCP responses.
- Logging: prefer logging module, avoid print in library code. Keep messages concise and testable.

Agents operating here

- Follow repo scripts in pyproject.toml for entrypoints.
- No Copilot or Cursor rules found; if added later, include .github/copilot-instructions.md and .cursor/rules/* in this file.
- Store all notes about progress being make and plans in the `notes/` directory

MCP SDK 2.x (see notes/mcp2-port.md)

- The server class is `MCPServer` (`mcp.server.mcpserver`), not `FastMCP`.
- There is no `Context.client_id`; scope per-client state with `_client_key(ctx)`, which reads the transport session (`mcp-session-id` header, else the SSE `session_id` query parameter, else a constant for stdio).
- Transport options are arguments to `mcp.run(...)`, not `mcp.settings`.
- `_meta` is a TypedDict: read `meta.get("progress_token")`, not `meta.progressToken`.
- Do not key per-client state on `id(ctx.session)` / `id(ctx.request_context)`; v2 builds a fresh proxy per request.
- Keep the transport tests (`tests/test_transports.py`); they are what catch identity regressions.

Keep this file short and machine-readable; update when style tools change.
