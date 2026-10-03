// Everything /settings shows: the editable server settings (when the server
// has /api/settings), plus live Telegram and n8n status from the health feed —
// so the status is right even on an older server. Secrets never appear here,
// only whether one is set.
import "server-only";
import { connection } from "next/server";
import { apiConfigured, brainFetch } from "./api";

type Json = Record<string, unknown>;
const obj = (v: unknown): Json => (v && typeof v === "object" && !Array.isArray(v) ? (v as Json) : {});
const str = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : String(v));
const whenFmt = new Intl.DateTimeFormat("en-GB", { timeZone: process.env.BRAIN_TIMEZONE || "UTC", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const when = (iso: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : whenFmt.format(d);
};

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
  /** `at` is preformatted on the server (BRAIN_TIMEZONE) so SSR and hydration agree. */
  last?: { status: string; at?: string; findings?: number };
}

export interface N8nStatus {
  health: string;
  url?: string;
  message?: string;
  apiStatus: string;
  apiError?: string;
  source: string;
  workflows: { name: string; active: boolean }[];
}

export interface SettingsView {
  /** live: editable · legacy: server too old to edit here · demo · down */
  mode: "live" | "legacy" | "demo" | "down";
  sections: SettingsSection[];
  models: Record<string, string | number | null> | null;
  repoPath?: string;
  telegram: TelegramStatus | null;
  n8n: N8nStatus | null;
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
  const [settings, health] = await Promise.all([
    brainFetch<Json>("/api/settings", { fresh: true }),
    brainFetch<Json>("/api/web/health", { fresh: true }),
  ]);
  if (!settings && !health) return { mode: "down", sections: [], models: null, telegram: null, n8n: null };

  const orch = obj(health?.orchestration);
  const diag = obj(orch.diagnostics);
  const delivery = obj(diag.last_delivery);
  const svc = obj(obj(health?.services).n8n);
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
  const n8n: N8nStatus | null = health
    ? {
        health: str(svc.status) || "unknown",
        url: str(svc.url) || undefined,
        message: str(svc.message || svc.error) || undefined,
        apiStatus: str(orch.workflow_api_status) || "unknown",
        apiError: str(orch.workflow_api_error) || undefined,
        source: str(orch.workflow_source) || "none",
        workflows: (Array.isArray(orch.workflows) ? orch.workflows : []).map((w) => ({ name: str(obj(w).name), active: Boolean(obj(w).active) })),
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
    n8n,
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
          f("TELEGRAM_ALERT_COOLDOWN_SECONDS", "Repeat cooldown, s", "int", 21600, "", { min: 60, max: 604800 }),
        ],
      },
      {
        id: "n8n",
        title: "n8n automation",
        fields: [
          f("N8N_BASE_URL", "n8n URL (from the Brain server)", "url", "http://n8n:5678"),
          f("N8N_PUBLIC_URL", "n8n URL (in your browser)", "url", "http://localhost:5678"),
          f("N8N_API_KEY", "n8n API key", "secret", null, "", { set: false }),
        ],
      },
    ],
    models: { llm_provider: "openai", llm_model: "gpt-4.1-mini", embedding_provider: "openai", embedding_model: "text-embedding-3-small", embedding_dimension: 1536 },
    repoPath: "/home/you/code/project-brain",
    telegram: { enabled: true, credentials: true, last: { status: "sent", at: when("2026-10-02T03:30:04Z"), findings: 4 } },
    n8n: { health: "healthy", url: "http://n8n:5678", apiStatus: "missing_key", source: "repo", workflows: [{ name: "Nightly health", active: false }] },
  };
}
