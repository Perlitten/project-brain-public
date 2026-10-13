# Client connection examples

All examples are **templates**. Configure your client's secure environment or
replace paths, hosts and `pbk_your_scoped_key` only in client configuration outside
this checkout. Keep the tracked root `.mcp.json` secret-free; do not overwrite it
with a personalized template.

| File | Client | Transport | Auth |
|---|---|---|---|
| `claude-code-stdio.mcp.json` | Claude Code | HTTP-backed stdio bridge | `BRAIN_API_URL` / `BRAIN_API_KEY` environment references |
| `claude-desktop-stdio.json` | Claude Desktop (Windows configuration layout) | local stdio | Local datastore configuration |
| `cursor-remote-http.json` | Cursor | Streamable HTTP (`/mcp`) | Bearer `pbk_` |
| `codex-remote-http.toml` | Codex | Streamable HTTP (`/mcp`) | Bearer `pbk_` |

The local stdio server requires running datastores and the checkout's `.env`;
see [MCP setup](../docs/MCP.md). The HTTP-backed bridge needs a configured API
endpoint and key, without local datastores. Native Windows installation is
unverified; the desktop JSON illustrates a configuration layout, not a tested
Windows installation path.

OAuth 2.1 clients (DCR + PKCE) connect to the same `/mcp` endpoint without any
static key — run `scripts/mint_api_credential.py --name <agent> --scopes
core:write,jobs:read` to mint scoped keys for the header-based setups instead.
