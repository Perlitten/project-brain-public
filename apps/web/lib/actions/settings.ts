"use server";
// Settings: typed writes to the server's .env, plus a one-click Telegram test.
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
