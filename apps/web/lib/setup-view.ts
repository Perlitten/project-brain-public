// Everything /setup needs to let you act on each step: the raw step status,
// the provider catalogue and current choice, and the address agents should
// use. Server-only; keys never appear here (only whether one is set).
import "server-only";
import { connection } from "next/server";
import { apiConfigured, brainFetch } from "./api";

type Json = Record<string, unknown>;

export interface StepView {
  id: string;
  title: string;
  done: boolean;
  /** API status word: done, todo, warn, blocked, … */
  status: string;
  detail: string;
  hint?: string;
  /** Step-specific fields from the API (services, llm, run, packs, …). */
  extra: Json;
}

export interface ProviderPreset {
  name: string;
  label: string;
  baseUrl?: string;
  requiresKey: boolean;
  keyEnv?: string;
  llmModel?: string;
  summarizerModel?: string;
  embeddingModel?: string;
  embeddingDimension?: number;
  embeddings: boolean;
}

export interface ProviderSlot {
  provider: string;
  native?: boolean;
  baseUrl?: string;
  model?: string;
  summarizerModel?: string;
  dimension?: number;
  keySet?: boolean;
  keySource?: string;
  requiresKey?: boolean;
}

export interface SetupView {
  mode: "live" | "demo" | "down";
  steps: StepView[];
  nextStep?: string;
  repoPath?: string;
  /** Null when the server predates the provider picker (404). */
  providers: { presets: ProviderPreset[]; llm?: ProviderSlot; embedding?: ProviderSlot } | null;
  /** The address an agent on another machine should use for BRAIN_API_URL. */
  publicApiUrl: string;
}

const str = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : String(v));
const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : undefined);
const obj = (v: unknown): Json => (v && typeof v === "object" && !Array.isArray(v) ? (v as Json) : {});

function publicUrl(): string {
  return (process.env.BRAIN_PUBLIC_API_URL || process.env.BRAIN_API_URL || "https://your-brain-server").trim().replace(/\/+$/, "");
}

function slot(v: unknown): ProviderSlot | undefined {
  const s = obj(v);
  if (!s.provider) return undefined;
  return {
    provider: str(s.provider),
    native: s.native === true || undefined,
    baseUrl: str(s.base_url) || undefined,
    model: str(s.model) || undefined,
    summarizerModel: str(s.summarizer_model) || undefined,
    dimension: num(s.dimension),
    keySet: typeof s.key_set === "boolean" ? s.key_set : undefined,
    keySource: str(s.key_source) || undefined,
    requiresKey: typeof s.requires_key === "boolean" ? s.requires_key : undefined,
  };
}

function preset(v: unknown): ProviderPreset {
  const p = obj(v);
  return {
    name: str(p.name),
    label: str(p.label) || str(p.display_name) || str(p.name),
    baseUrl: str(p.base_url) || undefined,
    requiresKey: p.requires_key !== false,
    keyEnv: str(p.key_env) || undefined,
    llmModel: str(p.llm_model) || undefined,
    summarizerModel: str(p.summarizer_model) || undefined,
    embeddingModel: str(p.embedding_model) || undefined,
    embeddingDimension: num(p.embedding_dimension),
    embeddings: p.embeddings === true,
  };
}

/** What a new agent key's config needs: the public API address and the repo path. */
export async function getAgentTarget(): Promise<{ publicApiUrl: string; repoPath?: string }> {
  if (!apiConfigured) {
    const demo = demoView();
    return { publicApiUrl: demo.publicApiUrl, repoPath: demo.repoPath };
  }
  await connection();
  const status = await brainFetch<Json>("/api/setup/status");
  return { publicApiUrl: publicUrl(), repoPath: str(status?.repo_path) || undefined };
}

export async function getSetupView(): Promise<SetupView> {
  if (!apiConfigured) return demoView();
  await connection();
  const [status, providers] = await Promise.all([
    brainFetch<Json>("/api/setup/status", { fresh: true }),
    brainFetch<Json>("/api/setup/providers", { fresh: true }),
  ]);
  if (!status) return { mode: "down", steps: [], providers: null, publicApiUrl: publicUrl() };
  const steps = (Array.isArray(status.steps) ? status.steps : []).map((raw): StepView => {
    const s = obj(raw);
    const { id, title, status: st, detail, hint, ...extra } = s;
    return {
      id: str(id),
      title: str(title) || str(id),
      done: str(st) === "done",
      status: str(st) || "todo",
      detail: str(detail),
      hint: str(hint) || undefined,
      extra,
    };
  });
  const current = obj(providers?.current);
  return {
    mode: "live",
    steps,
    nextStep: str(status.next_step) || undefined,
    repoPath: str(status.repo_path) || undefined,
    providers: providers
      ? {
          presets: (Array.isArray(providers.presets) ? providers.presets : []).map(preset).filter((p) => p.name),
          llm: slot(current.llm),
          embedding: slot(current.embedding),
        }
      : null,
    publicApiUrl: publicUrl(),
  };
}

// Demo: the same six steps with believable state, so the controls can be seen
// (they are locked — nothing runs without an API).
function demoView(): SetupView {
  const presets: ProviderPreset[] = [
    { name: "openai", label: "OpenAI", baseUrl: "https://api.openai.com/v1", requiresKey: true, keyEnv: "OPENAI_API_KEY", llmModel: "gpt-4o-mini", embeddingModel: "text-embedding-3-small", embeddingDimension: 1536, embeddings: true },
    { name: "openrouter", label: "OpenRouter", baseUrl: "https://openrouter.ai/api/v1", requiresKey: true, llmModel: "openai/gpt-4o-mini", embeddings: false },
    { name: "ollama", label: "Ollama (local)", baseUrl: "http://localhost:11434/v1", requiresKey: false, llmModel: "llama3.1", embeddingModel: "nomic-embed-text", embeddingDimension: 768, embeddings: true },
    { name: "openai_compatible", label: "Any OpenAI-compatible", requiresKey: true, embeddings: true },
  ];
  return {
    mode: "demo",
    repoPath: "/home/you/code/project-brain",
    nextStep: "agent",
    publicApiUrl: "https://brain.example.com",
    providers: {
      presets,
      llm: { provider: "openai", baseUrl: "https://api.openai.com/v1", model: "gpt-4o-mini", keySet: true, keySource: "LLM_API_KEY", requiresKey: true },
      embedding: { provider: "openai", baseUrl: "https://api.openai.com/v1", model: "text-embedding-3-small", dimension: 1536, keySet: true, keySource: "LLM_API_KEY", requiresKey: true },
    },
    steps: [
      { id: "services", title: "Start the services", done: true, status: "done", detail: "Postgres, Redis and Neo4j answer", extra: { services: { postgres: "ok", redis: "ok", neo4j: "ok" } } },
      { id: "repo", title: "Point Brain at your repository", done: true, status: "done", detail: "/home/you/code/project-brain", extra: {} },
      { id: "provider", title: "Choose the AI models", done: true, status: "done", detail: "Both providers answered a real call", extra: { llm: { provider: "openai", state: "verified" }, embedding: { provider: "openai", state: "verified" } } },
      { id: "indexed", title: "Index the code", done: true, status: "done", detail: "Last run completed", extra: { run: { run_id: 42, status: "completed", commit: "2a84512c0f1e", files: { processed: 1284, skipped: 12, failed: 0 } } } },
      { id: "agent", title: "Connect an agent", done: false, status: "todo", detail: "No agent has connected yet", hint: "Create a key below and paste the config into your agent", extra: { self_check: null, external_client: { connected: false } } },
      { id: "first_task", title: "Brief an agent on a real task", done: false, status: "todo", detail: "No context pack yet", extra: { packs: { usable: 0, missing: 0, empty: 0, stale: 0 } } },
    ],
  };
}
