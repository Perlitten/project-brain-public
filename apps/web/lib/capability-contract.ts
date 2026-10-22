export const CAPABILITY_NAMES = ["team", "team_bootstrap", "team_activity", "team_board", "knowledge_review", "repository_registration"] as const;
export type Capability = (typeof CAPABILITY_NAMES)[number];
export interface Capabilities { state: "demo" | "live" | "legacy" | "unavailable"; supported: Capability[] }
