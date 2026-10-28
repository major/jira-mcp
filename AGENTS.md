# Jira MCP ☁️

Read-only FastMCP server for Jira Cloud REST v3. Keep the API task-focused and
token-lean rather than mirroring every Jira endpoint. No Data Center support.

## Toolchain

- Python 3.15 and uv 0.13.5 are pinned. `lazy from ... import ...` is valid here.
- Run commands with `uv run --locked`. Run `make check` before declaring done.
- Ruff enforces Google docstrings, 120-character lines, and strict lint rules.
- `make coverage` requires 95% overall coverage and a separate 95% branch gate.
- `make audit` checks dependencies. Codecov is opt-in with `ENABLE_CODECOV=true`.

## Layout

- `src/jira_mcp/__init__.py`: imports tools for registration and provides `main()`.
- `app.py`: FastMCP server, pooled client lifespan, and configuration errors.
- `config.py`: Cloud URL validation and environment-based API token credentials.
- `client.py`: async HTTPX2 boundary, TLS, Basic auth, and safe errors.
- `tools.py`: read-only tools, bounded arguments, and upstream response validation.
- `text.py`: best-effort ADF text extraction with optional raw recovery.

## Development rules 🧪

- Write behavioral tests first. Use the real in-memory FastMCP client and native
  `httpx2.MockTransport`. RESPX targets HTTPX, not HTTPX2.
- Tests block outbound sockets. Never use real credentials, sleeps, or busy loops.
- Use Arrange-Act-Assert, fixtures, and parameterization. Keep tests independent.
- Keep field IDs and original custom-field values. Names are not unique.
- Preserve pagination, truncation indicators, and optional unchanged ADF.
- Never expose token values, upstream error bodies, or transport exception text.
- Keep the README tool table and server instructions aligned with public tools.
- Prefer simple code and the Rule of Three over speculative abstractions.
- Write US English, add docstrings, and do not use em dashes.
