// Agents with a name Brain can file under get their own page (/agents/<name>).
const AGENT_NAME = /^[A-Za-z0-9][A-Za-z0-9 ._@-]{0,119}$/;
const RESERVED = new Set(["agent", "unknown", "an agent"]);

export const agentHref = (name: string): string | null =>
  AGENT_NAME.test(name) && !RESERVED.has(name.toLowerCase()) ? `/agents/${encodeURIComponent(name)}` : null;
