# Jira MCP ☁️

Task-focused FastMCP server for Jira Cloud REST v3 with explicit reads and writes.
Keep the API token-lean rather than mirroring every Jira endpoint. No Data Center support.

## Toolchain

- Python 3.15 and uv 0.13.5 are pinned. `lazy from ... import ...` is valid here.
- Run commands with `uv run --locked`. Run `make check` before declaring done.
- Ruff enforces Google docstrings, 120-character lines, and strict lint rules.
- `make coverage` requires 95% overall coverage and a separate 95% branch gate.
- `make audit` checks dependencies. Codecov is opt-in with `ENABLE_CODECOV=true`.

## Layout

- `src/jira_mcp/__init__.py`: imports tools for registration and provides `main()`.
- `app.py`: FastMCP server, pooled client lifespan, and configuration errors.
- `instructions.py`: agent-facing server instructions, including each tool's main arguments.
- `config.py`: Cloud URL validation and environment-based API token credentials.
- `client.py`: async HTTPX2 boundary, TLS, Basic auth, and safe errors.
- `common.py`: shared argument types, tool annotations, and payload helpers.
- `fields.py`: field catalog models and the lifespan catalog cache.
- `create_metadata.py`: create-metadata models and pure checks for planned issue fields.
- `text.py`: plain text to ADF and best-effort extraction with optional raw recovery.
- `tools/`: one module per area (`users`, `issues`, `comments`, `fields`, `create`, `transitions`, `links`).
  Every `@mcp.tool` lives here, and `tools/__init__.py` imports each module so it registers.

## Adding a tool ✅

- Put it in the matching `tools/<area>.py`. For a new area, add a module and import it in `tools/__init__.py`.
- Tool modules must not import each other. Move a shared helper to `common.py` only when a second module needs it.
- Test it in the matching `tests/test_<area>.py`. Keep cross-tool checks in `tests/test_server.py`.
- Add it to `TOOLS` in `tests/test_tool_inventory.py`, the README tool table, and the instructions' main arguments.

## Development rules 🧪

- Write behavioral tests first. Use the real in-memory FastMCP client and native
  `httpx2.MockTransport`. RESPX targets HTTPX, not HTTPX2.
- Tests block outbound sockets. Never use real credentials, sleeps, or busy loops.
- Use Arrange-Act-Assert, fixtures, and parameterization. Keep tests independent.
- Shared fixtures (`jira_env`, `mock_jira`) live in `tests/conftest.py`; call tools with `tests/helpers.py:invoke`.
- `tests/test_tool_inventory.py` checks that registered tools, annotations, README rows, and instruction arguments agree.
- Keep field IDs and original custom-field values. Names are not unique.
- Preserve pagination, truncation indicators, and optional unchanged ADF.
- Never expose token values, upstream error bodies, or transport exception text. `mask_error_details=True` hides
  unexpected exception text, but `ToolError` messages always reach the caller.
- Gate creation on the lifespan's issue-creation setting, never retry it automatically,
  and warn about uncertain outcomes before a caller considers a retry.
- Keep the README tool table and server instructions aligned with public tools.
- Prefer simple code and the Rule of Three over speculative abstractions.
- Write US English, add docstrings, and do not use em dashes.
