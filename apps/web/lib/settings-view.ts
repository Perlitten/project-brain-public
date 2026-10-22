// Everything /settings shows: the editable server settings (when the server
// has /api/settings), live Telegram status from the health feed, and the
// scheduled jobs from /scheduler/jobs. Secrets never appear here,
// only whether one is set.
import "server-only";
import { utcTime } from "./time";
import { connection } from "next/server";
import { apiConfigured, brainFetch } from "./api";
import { demoScheduler, getSchedulerJobs, type SchedulerView } from "./scheduler-view";

type Json = Record<string, unknown>;
const obj = (v: unknown): Json => (v && typeof v === "object" && !Array.isArray(v) ? (v as Json) : {});
const str = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : String(v));
const when = (iso: string) => iso ? utcTime(iso) : undefined;

export type SettingKind = "bool" | "int" | "float" | "text" | "url" | "secret";
export type SettingValue = string | number | boolean | null;

export interface SettingField {
  key: string;
  label: string;
  kind: SettingKind;
  help: string;
  value: SettingValue;
  /** Secrets only: whether one is saved. */
  set?: boolean;
  min?: number;
  max?: number;
  clearable: boolean;
  /** Also passed in the server's process environment, which wins on restart. */
  fromEnv: boolean;
}

export interface SettingsSection {
  id: string;
  title: string;
  fields: SettingField[];
}

export interface TelegramStatus {
  enabled: boolean;
  credentials: boolean;
  /** `at` is formatted in UTC so SSR and hydration agree. */
  last?: { status: string; at?: string; findings?: number };
}

export interface SettingsView {
  /** live: editable · legacy: server too old to edit here · demo · down */
  mode: "live" | "legacy" | "demo" | "down";
  sections: SettingsSection[];
  models: Record<string, string | number | null> | null;
  repoPath?: string;
  telegram: TelegramStatus | null;
  scheduler: SchedulerView | null;
  /** Self-diagnosis LLM stage, when it failed. */
  llmError?: string;
  version?: string;
}

function toField(raw: unknown): SettingField {
  const f = obj(raw);
  const v = f.value;
  return {
    key: str(f.key),
    label: str(f.label) || str(f.key),
    kind: (str(f.kind) || "text") as SettingKind,
    help: str(f.help),
    value: typeof v === "string" || typeof v === "number" || typeof v === "boolean" ? v : null,
    set: typeof f.set === "boolean" ? f.set : undefined,
    min: typeof f.min === "number" ? f.min : undefined,
    max: typeof f.max === "number" ? f.max : undefined,
    clearable: Boolean(f.clearable),
    fromEnv: Boolean(f.from_env),
  };
}

export async function getSettingsView(): Promise<SettingsView> {
  if (!apiConfigured) return demo();
  await connection();
  const [settings, health, scheduler] = await Promise.all([
    brainFetch<Json>("/api/settings", { fresh: true }),
    brainFetch<Json>("/api/web/health", { fresh: true }),
    getSchedulerJobs(),
  ]);
  if (!settings && !health) return { mode: "down", sections: [], models: null, telegram: null, scheduler: null };

  const orch = obj(health?.orchestration);
  const diag = obj(orch.diagnostics);
  const delivery = obj(diag.last_delivery);
  const llm = obj(obj(diag.last_result).llm);

  const telegram: TelegramStatus | null = health
    ? {
        enabled: Boolean(diag.telegram_enabled),
        credentials: Boolean(diag.telegram_credentials_present),
        last: delivery.status
          ? { status: str(delivery.status), at: delivery.evaluated_at ? when(str(delivery.evaluated_at)) : undefined, findings: typeof delivery.finding_count === "number" ? delivery.finding_count : undefined }
          : undefined,
      }
    : null;
  return {
    mode: settings ? "live" : "legacy",
    sections: (Array.isArray(settings?.sections) ? settings.sections : []).map((s) => ({
      id: str(obj(s).id),
      title: str(obj(s).title),
      fields: (Array.isArray(obj(s).fields) ? (obj(s).fields as unknown[]) : []).map(toField),
    })),
    models: settings ? (obj(settings.models) as Record<string, string | number | null>) : null,
    repoPath: str(settings?.repo_path) || undefined,
    telegram,
    scheduler,
    llmError: str(llm.status) === "failed" ? str(llm.error).split(" For more information")[0] : undefined,
    version: str(obj(diag.build).version) || undefined,
  };
}

function demo(): SettingsView {
  const f = (key: string, label: string, kind: SettingKind, value: SettingValue, help = "", extra: Partial<SettingField> = {}): SettingField => ({
    key,
    label,
    kind,
    value,
    help,
    clearable: kind === "secret",
    fromEnv: false,
    ...extra,
  });
  return {
    mode: "demo",
    sections: [
      {
        id: "telegram",
        title: "Telegram alerts",
        fields: [
          f("TELEGRAM_ALERTS_ENABLED", "Send alerts", "bool", true),
          f("TELEGRAM_ALERT_BOT_TOKEN", "Bot token", "secret", null, "", { set: true }),
          f("TELEGRAM_ALERT_CHAT_ID", "Chat ID", "text", "-1001234567890"),
          f("TELEGRAM_ALERT_COOLDOWN_SECONDS", "Critical reminder, s", "int", 86400, "", { min: 60, max: 604800 }),
        ],
      },
      {
        id: "automation",
        title: "Background checks",
        fields: [
          f("SELF_DIAGNOSIS_ENABLED", "Scheduled self-diagnosis", "bool", true, "Brain checks its own health on a schedule and reports findings."),
          f("SELF_DIAGNOSIS_USE_LLM", "Summarize diagnosis with the LLM", "bool", false, "Off = deterministic probes only, no model calls."),
          f("PROACTIVE_INSIGHTS_ENABLED", "Proactive insights", "bool", true, "Scheduled scan for risks and drift in the indexed code."),
          f("NIGHTLY_DEEP_MAINTENANCE_ENABLED", "Nightly deep maintenance", "bool", true, "Nightly re-scoring and quality checks of the index."),
        ],
      },
      {
        id: "search",
        title: "Search & context packs",
        fields: [
          f("RETRIEVAL_USE_LLM_RERANK", "LLM rerank", "bool", true, "Better ordering of search results, slower and costs model calls."),
          f("CONTEXT_PACK_SKIP_PLAN_LLM", "Skip the planning call", "bool", false, "Faster briefings without the model’s plan step."),
          f("RETRIEVAL_PACK_LIMIT", "Files per context pack", "int", 12, "Upper bound on files picked for one briefing.", { min: 1, max: 50 }),
        ],
      },
      {
        id: "indexing",
        title: "Indexing & workers",
        fields: [
          f("INDEX_FILE_CONCURRENCY", "Files in parallel", "int", 4, "Higher is faster on big machines.", { min: 1, max: 32 }),
          f("INDEX_PROVIDER_CONCURRENCY", "Model calls in parallel", "int", 2, "Keep low on rate-limited providers.", { min: 1, max: 16 }),
          f("INDEX_EMBEDDING_BATCH_SIZE", "Embedding batch size", "int", 64, "Texts per embeddings request.", { min: 1, max: 256 }),
          f("WORKER_JOB_TIMEOUT_SECONDS", "Job timeout, s", "int", 3600, "A background job is stopped after this long.", { min: 60, max: 21600 }),
        ],
      },
    ],
    models: { llm_provider: "openai", llm_model: "gpt-4.1-mini", embedding_provider: "openai", embedding_model: "text-embedding-3-small", embedding_dimension: 1536 },
    repoPath: "/home/you/code/project-brain",
    telegram: { enabled: true, credentials: true, last: { status: "sent", at: when("2026-10-02T03:30:04Z"), findings: 4 } },
    scheduler: demoScheduler(),
  };
}
