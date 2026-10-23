/** Brain's Streamable HTTP MCP endpoint, served by the API itself (apps/api/mcp_http.py). */
export const mcpUrl = (apiUrl: string) => `${apiUrl.replace(/\/+$/, "")}/mcp`;
