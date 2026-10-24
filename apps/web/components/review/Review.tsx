"use client";
// Review page controls: approve or reject a proposal someone else made, withdraw
// your own. Brain checks every call with the signed-in member's key and the
// version shown here, so a proposal someone else decided first fails cleanly.

import { useState } from "react";
import { Dialog, LockNote, RunButton, useAction } from "@/components/act";
import { Chip, Table } from "@/components/ui";
import { approveProposal, rejectProposal, withdrawProposal } from "@/lib/actions/review";
import type { ProposalRow, ProposalState } from "@/lib/review-view";
import type { Tone } from "@/lib/types";

const KIND_WORD: Record<ProposalRow["kind"], string> = { decision: "Decision", rule: "Rule", learning: "Learning" };
const STATE_TONE: Record<ProposalState, Tone> = { pending: "info", approved: "ok", rejected: "bad", withdrawn: "idle" };
const STATE_WORD: Record<ProposalState, string> = { pending: "waiting for review", approved: "approved", rejected: "rejected", withdrawn: "withdrawn" };

function Scope({ row }: { row: ProposalRow }) {
  return row.repoPath ? <span className="sub mono">{row.repoPath}</span> : <span className="sub">All projects (team admins review)</span>;
}

function What({ row }: { row: ProposalRow }) {
  return (
    <>
      <span>
        {KIND_WORD[row.kind]}
        {row.edit ? " edit" : ""}: {row.title}
      </span>
      {row.summary && <span className="sub">{row.summary.length > 160 ? `${row.summary.slice(0, 157)}…` : row.summary}</span>}
      <Scope row={row} />
    </>
  );
}

function RejectButton({ row }: { row: ProposalRow }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const { run, pending, mode } = useAction(rejectProposal, { title: "Reject" });
  return (
    <>
      <button type="button" className="btn btn--neutral btn--sm" disabled={pending || mode === "locked"} onClick={() => setOpen(true)}>
        Reject
      </button>
      <Dialog open={open} onClose={() => setOpen(false)} title={`Reject “${row.title}”?`}>
        <label className="field">
          <span className="field__label">Reason (optional)</span>
          <textarea className="input" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={2000} rows={3} />
          <span className="field__hint">The author sees it with the proposal.</span>
        </label>
        <div className="dialog__actions">
          <button type="button" className="btn btn--neutral" onClick={() => setOpen(false)}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn--danger"
            disabled={pending || mode === "locked"}
            onClick={async () => {
              const res = await run(row.id, row.version, reason);
              if (res.ok) setOpen(false);
            }}
          >
            Reject
          </button>
        </div>
      </Dialog>
    </>
  );
}

/** Pending proposals the signed-in member may review. Their own proposals are listed but cannot be approved. */
export function ReviewQueue({ rows, selfId }: { rows: ProposalRow[]; selfId: number | null }) {
  return (
    <>
      <Table
        caption="Proposals waiting for review"
        rows={rows}
        rowKey={(r) => String(r.id)}
        filter={{ search: (r) => `${r.title} ${r.summary} ${r.repoPath ?? ""}`, facet: { label: "Kind", of: (r) => r.kind }, noun: ["proposal", "proposals"] }}
        columns={[
          { head: "Proposed change", cell: (r) => <What row={r} /> },
          { head: "Author", cell: (r) => (r.authorId === null ? "—" : r.authorId === selfId ? "you" : `#${r.authorId}`) },
          { head: "Proposed", cell: (r) => r.createdAt ?? "—" },
          {
            head: "Decision",
            cell: (r) =>
              r.authorId !== null && r.authorId === selfId ? (
                <span className="text-faint">Another reviewer decides</span>
              ) : (
                <div className="row-actions">
                  <RunButton
                    small
                    variant="primary"
                    label="Approve"
                    title="Approve"
                    action={() => approveProposal(r.id, r.version)}
                    confirm={{ title: `Approve “${r.title}”?`, body: "It becomes approved knowledge that agents receive in context packs. The author stays on record, with you as the reviewer.", yes: "Approve" }}
                  />
                  <RejectButton row={r} />
                </div>
              ),
          },
        ]}
      />
      <LockNote />
    </>
  );
}

/** The signed-in member's own proposals, with a way to take back a pending one. */
export function MyProposals({ rows }: { rows: ProposalRow[] }) {
  return (
    <Table
      caption="Your proposals"
      rows={rows}
      rowKey={(r) => String(r.id)}
      columns={[
        { head: "Proposed change", cell: (r) => <What row={r} /> },
        {
          head: "Status",
          cell: (r) => (
            <>
              <Chip tone={STATE_TONE[r.state]}>{STATE_WORD[r.state]}</Chip>
              {r.reason && <span className="sub">{r.reason}</span>}
            </>
          ),
        },
        { head: "Proposed", cell: (r) => r.createdAt ?? "—" },
        {
          head: "",
          cell: (r) =>
            r.state === "pending" ? (
              <RunButton small label="Withdraw" title="Withdraw" action={() => withdrawProposal(r.id, r.version)} />
            ) : (
              <span className="text-faint">{r.decidedAt ?? ""}</span>
            ),
        },
      ]}
    />
  );
}
