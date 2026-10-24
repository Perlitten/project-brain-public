"use client";
// Me page: the caller's own review notifications and their Telegram link. Brain
// answers every call for the signed-in member only, so these controls can never
// read or change another member's inbox or chat.

import Link from "next/link";
import { useState } from "react";
import { LockNote, RunButton, useAction } from "@/components/act";
import { Chip, Table } from "@/components/ui";
import {
  markAllNotificationsRead,
  markNotificationRead,
  startTelegramLink,
  unlinkTelegram,
  verifyTelegramLink,
} from "@/lib/actions/notifications";
import type { NotificationRow, TelegramLinkView } from "@/lib/team-notifications-view";

/** Brain stores UTC; minutes are enough here. */
const when = (iso: string | null) => (iso ? `${iso.slice(0, 16).replace("T", " ")} UTC` : "—");

export function NotificationInbox({ rows, unread }: { rows: NotificationRow[]; unread: number }) {
  return (
    <>
      <Table<NotificationRow>
        rows={rows}
        rowKey={(n) => String(n.id)}
        caption="Your notifications"
        columns={[
          {
            head: "Notification",
            cell: (n) => (
              <>
                {!n.readAt && (
                  <>
                    <Chip tone="info">new</Chip>{" "}
                  </>
                )}
                {n.summary}
              </>
            ),
          },
          {
            head: "Proposal",
            cell: (n) =>
              n.proposalId === null ? (
                "—"
              ) : n.proposalState === "pending" ? (
                <Link href="/review">Review #{n.proposalId}</Link>
              ) : (
                <>
                  #{n.proposalId} <Chip tone="idle">{n.proposalState || "gone"}</Chip>
                </>
              ),
          },
          { head: "When", cell: (n) => when(n.createdAt) },
          {
            head: "",
            cell: (n) => (n.readAt ? <span className="field__hint">read</span> : <RunButton small label="Mark read" title="Notifications" action={() => markNotificationRead(n.id)} />),
          },
        ]}
      />
      {unread > 0 && (
        <div className="btn-row">
          <RunButton label={`Mark all ${unread} read`} title="Notifications" action={markAllNotificationsRead} />
        </div>
      )}
      <LockNote />
    </>
  );
}

export function TelegramLinkForm({ link }: { link: TelegramLinkView }) {
  const [chat, setChat] = useState("");
  const [code, setCode] = useState("");
  const start = useAction(startTelegramLink, { title: "Link Telegram" });
  const verify = useAction(verifyTelegramLink, { title: "Link Telegram" });
  return (
    <div className="form">
      {link.linked && (
        <div className="signin__state">
          <Chip tone="ok">Linked</Chip>
          <p>
            Review notices go to chat {link.chat}
            {link.linkedAt ? `, linked ${when(link.linkedAt)}` : ""}.
          </p>
          <RunButton
            label="Unlink"
            variant="danger"
            title="Telegram"
            action={unlinkTelegram}
            confirm={{ title: "Unlink Telegram?", body: "Review notices stop reaching this chat. Your inbox here is unchanged.", yes: "Unlink" }}
          />
        </div>
      )}
      <form
        className="form__row"
        autoComplete="off"
        onSubmit={async (e) => {
          e.preventDefault();
          if ((await start.run(chat)).ok) setChat("");
        }}
      >
        <label className="field">
          <span className="field__label">{link.linked ? "Link a different chat" : "Your Telegram chat id"}</span>
          <input className="input" inputMode="numeric" value={chat} onChange={(e) => setChat(e.target.value)} placeholder="123456789" maxLength={21} spellCheck={false} />
          <span className="field__hint">Start a chat with this Brain’s bot first, then enter your numeric chat id. The bot sends a code to that chat.</span>
        </label>
        <div className="field">
          <span className="field__label">&nbsp;</span>
          <button type="submit" className="btn btn--neutral" disabled={start.pending || start.mode === "locked" || !chat.trim()}>
            Send code
          </button>
        </div>
      </form>
      {link.pending && (
        <form
          className="form__row"
          autoComplete="off"
          onSubmit={async (e) => {
            e.preventDefault();
            if ((await verify.run(code)).ok) setCode("");
          }}
        >
          <label className="field">
            <span className="field__label">Code sent to chat {link.pendingChat}</span>
            <input className="input" inputMode="numeric" value={code} onChange={(e) => setCode(e.target.value)} placeholder="6-digit code" maxLength={6} spellCheck={false} />
            <span className="field__hint">It expires at {when(link.codeExpiresAt)}. After five wrong tries, ask for a new one.</span>
          </label>
          <div className="field">
            <span className="field__label">&nbsp;</span>
            <button type="submit" className="btn btn--primary" disabled={verify.pending || verify.mode === "locked" || !code.trim()}>
              Link chat
            </button>
          </div>
        </form>
      )}
      <LockNote />
    </div>
  );
}
