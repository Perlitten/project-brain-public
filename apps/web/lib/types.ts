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
}

export interface LedgerEvent {
  at: string;
  tone: Tone;
  source: string;
  text: string;
  ref?: string;
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
}

export interface Decision {
  id: string;
  title: string;
  status: "accepted" | "proposed" | "superseded";
  scope: string;
  recordedAt: string;
  summary: string;
}

export interface Rule {
  id: string;
  rule: string;
  severity: "block" | "warn" | "advise";
  scope: string;
  /** Unknown when the API does not count rule hits. */
  hits30d?: number;
}

export interface Insight {
  id: string;
  tone: Tone;
  kind: "drift" | "coupling" | "hotspot" | "freshness" | "other";
  title: string;
  evidence: string;
  module: string;
  detectedAt: string;
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
  ndcgLift: string;
  queries24h: number;
  /** What `queries24h` counts over, when it is not the last 24 hours. */
  queriesWindow?: string;
  /** False when the live API returned no reranker status at all. */
  available?: boolean;
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
