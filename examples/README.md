# Client connection examples

All examples are **templates** — replace paths, hosts and `pbk_your_scoped_key`
with real values. Never commit a literal key: prefer `${ENV_VAR}` references.

| File | Client | Transport | Auth |
|---|---|---|---|
| `claude-code-stdio.mcp.json` | Claude Code / Claude Desktop | stdio, local server | `BRAIN_API_KEY` env var |
| `claude-desktop-stdio.json` | Claude Desktop (Windows layout) | stdio | scoped `pbk_` key |
| `cursor-remote-http.json` | Cursor | Streamable HTTP (`/mcp`) | Bearer `pbk_` |
| `codex-remote-http.toml` | Codex | Streamable HTTP (`/mcp`) | Bearer `pbk_` |

OAuth 2.1 clients (DCR + PKCE) connect to the same `/mcp` endpoint without any
static key — run `scripts/mint_api_credential.py --name <agent> --scopes
core:write,jobs:read` to mint scoped keys for the header-based setups instead.
