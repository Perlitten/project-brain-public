export type Tone = "ok" | "warn" | "bad" | "info" | "idle";

export type JobStatus =
  | "completed"
  | "failed"
  | "error"
  | "queued"
  | "skipped"
  | "degraded"
  | "cancelled"
  | "running"
  | "retrying"
  /** Closed by the lifecycle reaper after its deadline; the work itself may have finished. */
  | "timed_out";

export interface Repository {
  id: number;
  slug: string;
  name: string;
  path: string;
  branch: string;
  head: string;
  behind: number | null;
  freshness?: string;
  /** The scheduled origin check (git ls-remote), when it has run for this repository. */
  originPoll?: OriginPoll;
}

export interface OriginPoll {
  /** ok · error · unconfigured */
  status: string;
  /** Formatted in UTC. */
  at?: string;
  error?: string;
  /** Credentials already masked by the server. */
  remote?: string;
  /** Commits on origin that the index does not have yet. */
  behind: number | null;
}

export interface ConditionTile {
  value: string;
  label: string;
  tone: Tone;
}

export interface Condition {
  tone: Tone;
  headline: string;
  detail: string;
  action?: { label: string; hint: string };
  tiles: ConditionTile[];
}

export type MeterTone = "filled" | "missing" | "excluded";

export interface Meter {
  label: string;
  value: string;
  parts: { tone: MeterTone; count: number; text: string }[];
}

export interface Corpus {
  files: number;
  chunks: number;
  symbols: number;
  embeddings: number;
  meters: Meter[];
  available?: boolean;
  availabilityNote?: string;
  /** Working-tree edits omitted by the latest index run, not a live checkout count. */
  uncommitted?: number | null;
}

export interface Job {
  id: string;
  kind: string;
  status: JobStatus;
  repo: string;
  startedAt: string;
  duration?: string;
  detail?: string;
}

export interface AgentRun {
  id: string;
  agent: string;
  task: string;
  status: JobStatus;
  /** Unknown when the source does not record token use. */
  tokens?: number;
  /** Unknown when the source does not record whether a context pack was used. */
  packHit?: boolean;
  startedAt: string;
  duration: string;
  /** Demo data only: repository slug this task ran on. */
  repo?: string;
  /** The full task id, when the source has one (the row's `id` may be shortened). */
  taskId?: string;
  /** Compare-and-swap version a cancel must send; absent when the source has none. */
  version?: number;
  /** Brain's own status word (e.g. "validating"), finer than `status`. */
  rawStatus?: string;
  /** Someone already asked Brain to stop this task. */
  cancelRequested?: boolean;
}

export interface LedgerEvent {
  at: string;
  tone: Tone;
  source: string;
  text: string;
  ref?: string;
  /** Where the ref leads, when Brain named the entity. */
  href?: string;
}

export interface IndexRun {
  id: number;
  revision: string;
  /** "unknown" when the API does not record what started the run. */
  trigger: "push" | "schedule" | "manual" | "auto-heal" | "unknown";
  status: JobStatus;
  completeness: "complete" | "partial" | "aborted";
  files: number;
  changed: number;
  chunks: number;
  startedAt: string;
  duration: string;
  /** Edited-but-uncommitted files the run left out (it indexed the committed HEAD). */
  uncommitted?: number;
}

export interface ContextPack {
  id: string;
  task: string;
  budget: "small" | "medium" | "large";
  tokens: number;
  files: number;
  createdAt: string;
  /** null: Brain can't tell (the pack doesn't record which commit it was built from). */
  stale: boolean | null;
  consumer: string;
  /** Memory the pack carried, when Brain recorded its contents (null: not recorded). */
  memory?: { rules: number | null; decisions: number | null; learnings: number | null };
  /** Repository slug this pack was built for; absent on legacy/global packs. */
  repo?: string;
}

export interface Decision {
  id: string;
  title: string;
  status: "accepted" | "proposed" | "superseded" | "validated_unmerged";
  scope: string;
  recordedAt: string;
  summary: string;
  /** Stored repository path; null means the decision applies to every project. */
  repoPath?: string | null;
  /** Demo data only: repository slug this decision belongs to. */
  repo?: string;
  /** How to address the stored row when editing it; absent on history copies. */
  write?: DecisionWrite;
}

/** The stored decision a write replaces: Brain keys decisions by title and scope. */
export interface DecisionWrite {
  title: string;
  repoPath: string | null;
  description: string;
  reason: string;
  /** As stored (for example proposed); edits must not implicitly accept it. */
  status: string;
  /** Compare-and-swap: the version this page read. */
  version: number;
}

/** The stored rule a write replaces. A rule write sends every field, so all of them travel. */
export interface RuleWrite {
  ruleId: string;
  name: string;
  repoPath: string | null;
  description: string;
  type: string;
  /** As stored (e.g. "critical"); the page groups it into block/warn/advise. */
  severity: string;
  status: string;
  appliesTo: Record<string, unknown> | null;
  version: number;
}

export interface Rule {
  id: string;
  rule: string;
  severity: "block" | "warn" | "advise";
  scope: string;
  /** Context packs this rule went into over the last 30 days: did it reach anyone. */
  delivered30d?: number;
  /** Diff reviews that cited this rule over the same window: did it catch anything. */
  caught30d?: number;
  /** Stored repository path; null means the rule applies to every project. */
  repoPath?: string | null;
  /** Demo data only: repository slug this rule belongs to. */
  repo?: string;
  /** How to address the stored row when editing it; absent on history copies. */
  write?: RuleWrite;
}

export interface Insight {
  id: string;
  tone: Tone;
  kind: "drift" | "coupling" | "hotspot" | "freshness" | "other";
  title: string;
  evidence: string;
  module: string;
  detectedAt: string;
  lastSeenAt?: string;
  occurrences?: number;
  dedupeKey?: string;
  codeAvailable?: boolean;
}

export interface ModuleEdge {
  from: string;
  to: string;
  weight: number;
  violation: boolean;
}

export interface McpTool {
  name: string;
  surface: "local" | "remote";
  /** Usage metrics are optional: the live API lists tools without traffic stats. */
  calls24h?: number;
  p95ms?: number;
  errorRate?: number;
  status: Tone;
}

export interface Report {
  id: string;
  title: string;
  kind: string;
  createdAt: string;
  size: string;
}

export interface Principal {
  id: string;
  name: string;
  kind: "human" | "agent" | "service";
  scopes: string[];
  lastSeen: string;
  credentials: number;
}

// Access: every identity (disabled ones too) with the full life of each key.
export type KeyStatus = "active" | "expired" | "revoked";

export interface AccessKey {
  current?: boolean;
  id: number;
  scopes: string[];
  status: KeyStatus;
  created: string;
  /** "never", or a date (with "in 12d" while the key is active). */
  expires: string;
  lastUsed: string;
  /** Active and running out within 14 days. */
  expiringSoon: boolean;
  /** Active and never presented to Brain. */
  neverUsed: boolean;
}

export interface Identity {
  current?: boolean;
  id: number;
  name: string;
  kind: "human" | "agent" | "service";
  disabled: boolean;
  createdAt: string;
  /** Keys that still work (not revoked, not expired). */
  activeKeys: number;
  /** Union of the active keys' permissions. */
  scopes: string[];
  lastSeen: string;
  /** Newest first. */
  keys: AccessKey[];
}

export interface AccessView {
  /** False when Brain wouldn't list identities (no admin key, or it's down). */
  readable: boolean;
  identities: Identity[];
  summary: { identities: number; disabled: number; activeKeys: number; expiringSoon: number; neverUsed: number };
}

export interface SetupStep {
  id: string;
  title: string;
  detail: string;
  done: boolean;
  command?: string;
}

export interface RerankerStatus {
  model: string;
  state: Tone;
  stateText: string;
  coverage: Meter;
  p50ms: number;
  p95ms: number;
  queries24h: number;
  /** What `queries24h` counts over, when it is not the last 24 hours. */
  queriesWindow?: string;
  /** False when the live API returned no reranker status at all. */
  available?: boolean;
  /** Why the status is unavailable, when the server refused the request for this project (not an outage). */
  unavailableReason?: string;
  /** True when the optional reranker was never set up on this install (nothing switched on, nothing encoded). */
  notSetUp?: boolean;
  /** Whether the model service itself answers (separate from being switched on). */
  service?: { tone: Tone; text: string };
  /** Plain reasons it is not re-ranking searches; empty when it is. */
  offReasons?: string[];
  /** The encoded index next to the one approved for live traffic. */
  index?: { revision: string; documents: number; updated?: string; approvedRevision?: string; approvedDocuments?: number };
}

export interface CodeModule {
  name: string;
  chunks: number;
  current: number;
  outdated: number;
  missing: number;
  excluded: number;
}
