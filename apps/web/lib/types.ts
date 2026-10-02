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
  | "retrying";

export interface Repository {
  id: number;
  slug: string;
  name: string;
  path: string;
  branch: string;
  head: string;
  behind: number;
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
  stale: boolean;
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
}

export interface CodeModule {
  name: string;
  chunks: number;
  current: number;
  outdated: number;
  missing: number;
  excluded: number;
}
