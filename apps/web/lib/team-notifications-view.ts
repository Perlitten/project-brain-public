// Data for the notifications inbox and Telegram link on the Me page. Read with the
// signed-in member's own key (lib/api.ts): Brain returns only the caller's own
// notifications and link, so nothing here filters other members out.
import "server-only";
import { brainRead } from "./api";

export interface NotificationRow {
  id: number;
  kind: string;
  summary: string;
  proposalId: number | null;
  /** The proposal's state now: "pending" until someone decides it. */
  proposalState: string | null;
  createdAt: string | null;
  readAt: string | null;
}
export type NotificationsResult =
  | { kind: "ok"; teamState: "solo" | "team"; unread: number; rows: NotificationRow[] }
  | { kind: "unavailable" };

export interface TelegramLinkView {
  kind: "ok";
  teamState: "solo" | "team";
  /** The server sends member notices (TEAM_MEMBER_TELEGRAM_NOTIFY and a bot token). */
  available: boolean;
  linked: boolean;
  /** Masked chat id, e.g. "…6789". */
  chat: string | null;
  linkedAt: string | null;
  pending: boolean;
  pendingChat: string | null;
  codeExpiresAt: string | null;
}
export type TelegramLinkResult = TelegramLinkView | { kind: "unavailable" };

const str = (v: unknown) => (typeof v === "string" ? v : "");
const num = (v: unknown) => (typeof v === "number" && Number.isInteger(v) ? v : null);

export function toNotificationRow(raw: unknown): NotificationRow | null {
  if (!raw || typeof raw !== "object") return null;
  const n = raw as Record<string, unknown>;
  const id = num(n.id);
  if (id === null || typeof n.kind !== "string") return null;
  return {
    id,
    kind: n.kind,
    summary: str(n.summary) || n.kind,
    proposalId: num(n.proposal_id),
    proposalState: str(n.proposal_state) || null,
    createdAt: str(n.created_at) || null,
    readAt: str(n.read_at) || null,
  };
}

export function toNotifications(raw: unknown): NotificationsResult {
  if (!raw || typeof raw !== "object") return { kind: "unavailable" };
  const d = raw as Record<string, unknown>;
  if (!Array.isArray(d.notifications)) return { kind: "unavailable" };
  return {
    kind: "ok",
    teamState: d.team_state === "team" ? "team" : "solo",
    unread: num(d.unread_count) ?? 0,
    rows: d.notifications.map(toNotificationRow).filter((r): r is NotificationRow => r !== null),
  };
}

export function toTelegramLink(raw: unknown): TelegramLinkResult {
  if (!raw || typeof raw !== "object") return { kind: "unavailable" };
  const d = raw as Record<string, unknown>;
  if (typeof d.linked !== "boolean") return { kind: "unavailable" };
  return {
    kind: "ok",
    teamState: d.team_state === "team" ? "team" : "solo",
    available: d.available === true,
    linked: d.linked,
    chat: str(d.chat) || null,
    linkedAt: str(d.linked_at) || null,
    pending: d.pending === true,
    pendingChat: str(d.pending_chat) || null,
    codeExpiresAt: str(d.code_expires_at) || null,
  };
}

export async function getNotifications(): Promise<NotificationsResult> {
  const res = await brainRead<unknown>("/team/me/notifications?limit=50", { fresh: true });
  return res.ok ? toNotifications(res.data) : { kind: "unavailable" };
}

export async function getTelegramLink(): Promise<TelegramLinkResult> {
  const res = await brainRead<unknown>("/team/me/telegram", { fresh: true });
  return res.ok ? toTelegramLink(res.data) : { kind: "unavailable" };
}
