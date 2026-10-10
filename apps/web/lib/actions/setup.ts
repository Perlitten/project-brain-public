"use server";
// Setup: every step on /setup can be done from the page itself.
import { mutate } from "./gate";
import type { ActionResult } from "./types";

const SETUP = ["/setup", "/"];

export async function saveRepoPath(path: string): Promise<ActionResult> {
  const value = path.trim();
  if (!value) return { ok: false, message: "Type the folder path of your repository first." };
  return mutate("/api/setup/config", {
    body: { target_repo_path: value },
    success: "Repository saved. Next: index it so Brain can read the code.",
    revalidate: SETUP,
  });
}

export interface ProviderInput {
  slot: "llm" | "embedding";
  provider: string;
  baseUrl?: string;
  model?: string;
  summarizerModel?: string;
  dimension?: number;
  /** Write-only. Blank keeps the key Brain already has. */
  apiKey?: string;
  clearApiKey?: boolean;
}

type ProviderReply = { applied?: string[]; api_key_written?: boolean; dimension_changed?: boolean; notes?: string[] };

export async function saveProvider(input: ProviderInput): Promise<ActionResult<ProviderReply>> {
  if (!input.provider) return { ok: false, message: "Pick a provider first." };
  const res = await mutate<ProviderReply>("/api/setup/provider", {
    body: {
      slot: input.slot,
      provider: input.provider,
      base_url: input.baseUrl,
      model: input.model,
      summarizer_model: input.slot === "llm" ? input.summarizerModel : undefined,
      dimension: input.slot === "embedding" ? input.dimension : undefined,
      api_key: input.apiKey?.trim() || undefined,
      clear_api_key: Boolean(input.clearApiKey),
    },
    success: (d) =>
      `${input.slot === "llm" ? "Language model" : "Embeddings"} saved${d.api_key_written ? " with the new key" : ""}. Press “Verify” to test it for real.`,
    revalidate: SETUP,
  });
  // Never keep the key around in anything returned to the browser.
  return res;
}

type ProbeSide = { provider?: string; state?: string; detail?: string };

export async function verifyProviders(): Promise<ActionResult<{ llm?: ProbeSide; embedding?: ProbeSide }>> {
  return mutate<{ llm?: ProbeSide; embedding?: ProbeSide }>("/api/setup/verify-provider", {
    body: {},
    timeoutMs: 60_000,
    success: (d) => {
      const say = (name: string, s?: ProbeSide) =>
        !s ? "" : s.state === "verified" ? `${name} works` : s.state === "demo" ? `${name} is in demo mode` : `${name} failed: ${s.detail ?? s.state}`;
      return [say("Language model", d.llm), say("Embeddings", d.embedding)].filter(Boolean).join(" · ") || "Providers checked.";
    },
    revalidate: SETUP,
  });
}

export async function verifyAgent(): Promise<ActionResult> {
  return mutate<Record<string, unknown>>("/api/setup/verify-agent", {
    body: {},
    timeoutMs: 60_000,
    success: (d) =>
      d.available === false
        ? `Self-check failed: ${String(d.error ?? "the agent tools didn’t start")}`
        : "Agent tools started and answered. Now add the config below to your agent.",
    revalidate: SETUP,
  });
}

export async function buildFirstPack(task: string, repoPath?: string): Promise<ActionResult<{ id?: number; path?: string; files?: string[] }>> {
  const text = task.trim();
  if (text.length < 8) return { ok: false, message: "Describe a real task in a sentence — e.g. “Add rate limiting to the login endpoint”." };
  const res = await mutate<Record<string, unknown>>("/context", {
    body: { task_description: text, repo_path: repoPath || undefined, persist: true },
    timeoutMs: 120_000,
    success: (d) => {
      const files = Array.isArray(d.retrieved_files) ? d.retrieved_files.length : 0;
      return `Context pack built — ${files} file${files === 1 ? "" : "s"} picked for this task.`;
    },
    revalidate: [...SETUP, "/packs"],
  });
  if (!res.ok || !res.data) return { ok: res.ok, message: res.message };
  const d = res.data;
  return {
    ok: true,
    message: res.message,
    data: {
      id: typeof d.id === "number" ? d.id : undefined,
      path: typeof d.path === "string" ? d.path : undefined,
      files: Array.isArray(d.retrieved_files)
        ? d.retrieved_files.map((f) => String((f as { path?: unknown }).path ?? "")).filter(Boolean).slice(0, 12)
        : [],
    },
  };
}
