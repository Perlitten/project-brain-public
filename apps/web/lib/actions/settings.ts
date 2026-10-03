"use server";
// Settings: typed writes to the server's .env, plus one-click connection tests.
import { mutate } from "./gate";
import type { ActionResult } from "./types";

type Value = string | number | boolean;
type SaveReply = { applied?: string[]; notes?: string[] };

export async function saveSettings(values: Record<string, Value>, clear: string[] = []): Promise<ActionResult<SaveReply>> {
  if (!Object.keys(values).length && !clear.length) return { ok: false, message: "Nothing changed." };
  return mutate<SaveReply>("/api/settings", {
    method: "PATCH",
    body: { values, clear },
    success: (d) => `Saved ${d.applied?.length ?? 0} setting${d.applied?.length === 1 ? "" : "s"}.`,
    revalidate: ["/settings"],
  });
}

type TelegramReply = { status?: string; detail?: string; enabled?: boolean };

export async function testTelegram(): Promise<ActionResult<TelegramReply>> {
  return mutate<TelegramReply>("/api/settings/test/telegram", {
    body: {},
    timeoutMs: 30_000,
    success: (d) =>
      d.status === "sent"
        ? `Test message sent — check the chat.${d.enabled ? "" : " Alerts themselves are still off."}`
        : d.status === "disabled"
          ? "Not sent: bot token or chat ID is missing."
          : `Telegram refused it (${d.detail ?? d.status}). Check the token and chat ID.`,
  });
}

type N8nReply = { health?: { status?: string; error?: string }; api_status?: string; workflows?: number; active?: number };

export async function testN8n(): Promise<ActionResult<N8nReply>> {
  return mutate<N8nReply>("/api/settings/test/n8n", {
    body: {},
    timeoutMs: 20_000,
    success: (d) => {
      const up = d.health?.status === "healthy" ? "n8n answers" : `n8n unreachable${d.health?.error ? ` (${d.health.error})` : ""}`;
      const api =
        d.api_status === "available"
          ? `API key works — ${d.active ?? 0} of ${d.workflows ?? 0} workflows active`
          : d.api_status === "missing_key"
            ? "no API key, so workflow state is unknown"
            : d.api_status === "auth_failed"
              ? "API key rejected"
              : `API: ${d.api_status}`;
      return `${up} · ${api}.`;
    },
    revalidate: ["/settings"],
  });
}
