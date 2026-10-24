export const CAPABILITY_NAMES = ["team", "team_bootstrap", "team_activity", "team_board", "knowledge_review", "repository_registration", "git_connect", "finding_actions", "request_traces", "request_query_history", "memory_current", "mcp_http"] as const;
export type Capability = (typeof CAPABILITY_NAMES)[number];
export interface Capabilities { state: "demo" | "live" | "legacy" | "unavailable"; supported: Capability[] }
