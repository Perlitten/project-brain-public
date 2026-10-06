// Every permission a Brain key can carry, grouped by what it touches, plus the
// presets Access offers. Collected from require_scope(...) in apps/api; the
// server treats "x:write" as also granting "x:read".

export interface ScopeArea {
  /** Scope prefix, e.g. "core" for core:read / core:write. */
  area: string;
  label: string;
  /** One plain sentence: what the area covers. */
  about: string;
  read: boolean;
  write: boolean;
}

export interface ScopeGroup {
  label: string;
  areas: ScopeArea[];
}

const rw = (area: string, label: string, about: string): ScopeArea => ({ area, label, about, read: true, write: true });
const ro = (area: string, label: string, about: string): ScopeArea => ({ area, label, about, read: true, write: false });

export const SCOPE_GROUPS: ScopeGroup[] = [
  {
    label: "Everyday",
    areas: [
      rw("core", "Code memory", "Search code, build context packs, read and record decisions and rules."),
      rw("jobs", "Background jobs", "Follow jobs; write starts re-indexing, health checks and other jobs."),
      rw("harness", "Agent task ledger", "Tasks the agent harness hands out and reports back on."),
      rw("setup", "Setup", "Setup status; write changes the repository and model providers."),
    ],
  },
  {
    label: "Insights",
    areas: [
      rw("graph", "Code graph", "The map of modules and calls; write rebuilds or rolls it back."),
      rw("drift", "Architecture drift", "Where code moves away from the agreed structure; write runs a scan."),
      rw("coupling", "Coupling", "Parts of the code that keep changing together."),
      rw("impact", "Change impact", "What a change touches — files, tests, call paths."),
      rw("freshness", "Freshness", "How current each repository's memory is; write rebuilds it."),
      rw("portfolio", "Portfolio", "Dependencies and cycles across several repositories."),
    ],
  },
  {
    label: "Changes and experiments",
    areas: [
      rw("remediation", "Fix plans", "Assisted plans for fixing findings."),
      rw("experiments", "Experiments", "Trying fixes side by side before a person applies one."),
      rw("lab", "Change lab", "Disposable workspaces where patches are tried out."),
      rw("execution", "Agent runs", "Phased agent execution sessions."),
      rw("shadow", "Shadow runs", "Agent runs replayed safely next to the real ones."),
      rw("autonomy", "Goals", "Long-running goals handed to agents."),
      rw("improvement", "Improvement", "Agent tournaments and promotion of better setups."),
      ro("routing", "Routing", "Which model or agent handles which request."),
    ],
  },
  {
    label: "Oversight",
    areas: [
      ro("ledger", "Evidence ledger", "The tamper-evident record of what Brain did."),
      ro("audit", "Audit log", "Every change made through the API, for review or export."),
      ro("control", "Control queue", "Decisions waiting for a person."),
      rw("operations", "Operations", "Nightly runs and incidents; write repairs vectors."),
      rw("workspace", "Repositories", "The repository registry; write adds or disables repositories."),
    ],
  },
  {
    label: "Admin",
    areas: [rw("principals", "Access", "Identities and keys; write creates, revokes and disables them.")],
  },
];

export const KNOWN_SCOPES = new Set(SCOPE_GROUPS.flatMap((g) => g.areas.flatMap((a) => [...(a.read ? [`${a.area}:read`] : []), ...(a.write ? [`${a.area}:write`] : [])])));

export interface ScopePreset {
  id: string;
  label: string;
  note: string;
  scopes: string[];
}

export const SCOPE_PRESETS: ScopePreset[] = [
  { id: "agent", label: "AI agent", note: "code memory + job status", scopes: ["core:write", "jobs:read"] },
  { id: "read", label: "Read-only", note: "search and read code memory", scopes: ["core:read"] },
  { id: "integration", label: "Integration (CI)", note: "read memory, start jobs", scopes: ["core:read", "jobs:write"] },
  { id: "admin", label: "Admin", note: "manage keys and setup", scopes: ["principals:read", "principals:write", "setup:read", "setup:write"] },
];

/** The same set of scopes, ignoring order. */
export const sameScopes = (a: string[], b: string[]) => a.length === b.length && [...a].sort().join(" ") === [...b].sort().join(" ");

export const EXPIRY_CHOICES: { days: number; label: string }[] = [
  { days: 0, label: "Never" },
  { days: 30, label: "In 30 days" },
  { days: 90, label: "In 90 days" },
  { days: 365, label: "In a year" },
];
