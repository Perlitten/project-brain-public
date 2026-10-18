"use server";
// Me page: mark review notifications read and link a Telegram chat. Every call
// goes out with the signed-in member's own key (lib/api.ts) and Brain acts on
// that member only: no request here can name another member's inbox or chat.
import { mutate } from "./gate";
import type { ActionResult } from "./types";

const ME = ["/team/me"];
const goodId = (n: number) => Number.isInteger(n) && n > 0;

export async function markNotificationRead(id: number): Promise<ActionResult> {
  if (!goodId(id)) return { ok: false, message: "That notification doesn’t look right. Reload the page." };
  const res = await mutate(`/team/me/notifications/${id}/read`, { success: "Marked as read.", revalidate: ME });
  return { ok: res.ok, message: res.message };
}

export async function markAllNotificationsRead(): Promise<ActionResult> {
  const res = await mutate<{ updated?: number }>("/team/me/notifications/read-all", {
    success: (d) => (d.updated ? `Marked ${d.updated} as read.` : "Nothing unread."),
    revalidate: ME,
  });
  return { ok: res.ok, message: res.message };
}

/** Asks the bot to send a one-time code to the chat; the link takes effect once the code is entered. */
export async function startTelegramLink(chatId: string): Promise<ActionResult> {
  const chat = chatId.trim();
  if (!/^-?\d{1,20}$/.test(chat)) return { ok: false, message: "Enter the numeric Telegram chat id, for example 123456789." };
  const res = await mutate("/team/me/telegram", {
    body: { chat_id: chat },
    success: "Code sent. Check Telegram and enter it below.",
    revalidate: ME,
  });
  return { ok: res.ok, message: res.message };
}

export async function verifyTelegramLink(code: string): Promise<ActionResult> {
  const clean = code.trim();
  if (!/^\d{6}$/.test(clean)) return { ok: false, message: "Enter the 6-digit code from the bot." };
  const res = await mutate("/team/me/telegram/verify", {
    body: { code: clean },
    success: "Telegram linked. Review notices now reach that chat.",
    revalidate: ME,
  });
  return { ok: res.ok, message: res.message };
}

export async function unlinkTelegram(): Promise<ActionResult> {
  const res = await mutate("/team/me/telegram", { method: "DELETE", success: "Telegram unlinked.", revalidate: ME });
  return { ok: res.ok, message: res.message };
}
