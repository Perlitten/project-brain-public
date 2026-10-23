import type { McpTool } from "./types";

/** Slow is a UI warning, not evidence of failure or a provider availability probe. */
export const SLOW_TOOL_MS = 2000;

export function toolSummary(tools: McpTool[]): { tone: "warn" | "ok" | "info" | "idle"; text: string } {
  const troubled = tools.filter((t) => t.status === "warn" || t.status === "bad").length;
  const measured = tools.filter((t) => (t.calls24h ?? 0) > 0 && t.status !== "info").length;
  if (!tools.length) return { tone: "idle", text: "No agent tools reported" };
  if (troubled) return { tone: "warn", text: `${troubled} tool${troubled === 1 ? "" : "s"} slow or failing; ${measured} of ${tools.length} used in the last 24 hours` };
  if (measured < tools.length) return { tone: "info", text: `${measured} of ${tools.length} tools used in the last 24 hours; remaining tools unverified` };
  return { tone: "ok", text: "All agent tools answered without reported warnings in the last 24 hours" };
}
