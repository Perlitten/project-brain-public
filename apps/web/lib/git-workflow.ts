// The push trigger for the reindex workflow, built from the branch the server
// accepts, so the GitHub side cannot quietly drift from Settings.

const HEADS = "refs/heads/";

export function webhookBranch(ref: unknown): string {
  const value = typeof ref === "string" ? ref.trim() : "";
  return value.startsWith(HEADS) && value.length > HEADS.length ? value.slice(HEADS.length) : "main";
}

// GitHub branch filters are globs: escape their special characters, then
// single-quote the pattern for YAML.
export function workflowTrigger(ref: unknown): string {
  const pattern = webhookBranch(ref).replace(/[*?+![\]\\]/g, "\\$&").replace(/'/g, "''");
  return `on:\n  push:\n    branches: ['${pattern}']`;
}
