"use client";
// Board controls: a kanban of tasks by stage, live leases, overlap warnings and
// the integration queue, with a conflict view that says what conflicts and what
// the signed-in member may do about it. Controls appear only where Brain would
// allow them (lib/board-model.ts); Brain still checks every call with the
// member's own key, the lease's fencing token and the entry's version.

import { useState } from "react";
import { CodeBlock, Dialog, LockNote, RunButton, useAction } from "@/components/act";
import { Chip, Table } from "@/components/ui";
import { integrateEntry, rejectEntry, releaseLease, renewLease, requestRun, type NewRun } from "@/lib/actions/board";
import {
  BOARD_COLUMNS, entryActions, instructionHref, knownBase, leaseActions, queuePosition,
  type BoardColumn, type BoardTask, type Lease, type Overlap, type ProjectBoard, type QueueEntry, type QueueStatus, type SupersededInstruction,
} from "@/lib/board-model";
import type { Tone } from "@/lib/types";
import "./board.css";

const STATUS_TONE: Record<QueueStatus, Tone> = { queued: "info", needs_rebase: "warn", stale_instructions: "warn", integrated: "ok", rejected: "idle" };
const STATUS_WORD: Record<QueueStatus, string> = { queued: "queued", needs_rebase: "needs rebase", stale_instructions: "stale instructions", integrated: "integrated", rejected: "rejected" };

const short = (sha: string | null) => (sha ? sha.slice(0, 12) : "—");
const when = (iso: string | null) => (iso ? iso.replace("T", " ").slice(0, 16) + " UTC" : "—");
const who = (id: number | null, selfId: number | null) => (id === null ? "unknown" : id === selfId ? "you" : `member #${id}`);
const anchor = (taskId: string) => `task-${taskId}`;

interface Ctx { board: ProjectBoard; selfId: number | null; title: (id: string) => string; has: (id: string) => boolean }

function TaskLink({ id, ctx, fallback }: { id: string; ctx: Ctx; fallback?: string | null }) {
  return <a href={`#${anchor(id)}`}>{ctx.has(id) ? ctx.title(id) : fallback || ctx.title(id)}</a>;
}

// ---------------------------------------------------------------------------
// Actions

function ReleaseButton({ lease, ctx }: { lease: Lease; ctx: Ctx }) {
  return (
    <RunButton
      small
      label="Release my lease"
      title="Release lease"
      action={() => releaseLease(ctx.board.id, lease.id, lease.fencingToken)}
      confirm={{ title: `Release ${lease.resource}?`, body: "Others can claim the path at once. Writes that still carry this fencing token are refused after release.", yes: "Release" }}
    />
  );
}

function LeaseControls({ lease, ctx }: { lease: Lease; ctx: Ctx }) {
  const can = leaseActions(lease, ctx.board.permissions, ctx.selfId);
  if (!can.release && !can.renew) return null;
  return (
    <div className="row-actions">
      {can.renew && <RunButton small label="Renew 5 min" title="Renew lease" action={() => renewLease(ctx.board.id, lease.id, lease.fencingToken, 300)} />}
      {can.release && <ReleaseButton lease={lease} ctx={ctx} />}
    </div>
  );
}

function RequestRunButton({ task, ctx }: { task: BoardTask; ctx: Ctx }) {
  const [run, setRun] = useState<NewRun | null>(null);
  const { run: go, pending, mode } = useAction(requestRun, { title: "Request run" });
  const [asking, setAsking] = useState(false);
  return (
    <>
      <button type="button" className="btn btn--neutral btn--sm" disabled={pending || mode !== "on" || !task.branch} onClick={() => setAsking(true)}>
        {pending ? <span className="spin" aria-hidden="true" /> : null}
        Request a fresh run
      </button>
      <Dialog open={asking} onClose={() => setAsking(false)} title={`Request a fresh run of “${task.title}”?`}>
        <p className="dialog__text">
          Brain resolves the current decisions and rules for the new run and refuses while dependencies are not done. The run starts from the task&apos;s
          declared base; rebase onto the current base in the agent before queueing it again.
        </p>
        <div className="dialog__actions">
          <button type="button" className="btn btn--neutral" onClick={() => setAsking(false)}>Cancel</button>
          <button
            type="button"
            className="btn btn--primary"
            disabled={pending || mode !== "on"}
            onClick={async () => {
              const res = await go(ctx.board.id, task.id, task.branch);
              setAsking(false);
              if (res.ok && res.data) setRun(res.data);
            }}
          >
            Request run
          </button>
        </div>
      </Dialog>
      <Dialog open={run !== null} onClose={() => setRun(null)} title="New run" wide>
        {run && (
          <div className="form form--wide">
            <p className="dialog__text">Run <span className="mono">{run.runId}</span> on branch <span className="mono">{task.branch}</span>.</p>
            {run.credential ? (
              <>
                <div className="callout tone-warn">
                  <b>Copy the run key now — Brain shows it only once.</b>
                  <span>Hand it to the agent that does the work; it carries only this run&apos;s permissions.</span>
                </div>
                <CodeBlock code={run.credential} label="Run key" secret />
              </>
            ) : (
              <p className="dialog__text">Brain returned an existing run for this request, so there is no new key to show.</p>
            )}
          </div>
        )}
      </Dialog>
    </>
  );
}

function IntegrateButton({ entry, ctx }: { entry: QueueEntry; ctx: Ctx }) {
  const known = knownBase(ctx.board, entry);
  const [open, setOpen] = useState(false);
  const [base, setBase] = useState(known?.source === "integrated" ? known.sha : "");
  const { run, pending, mode } = useAction(integrateEntry, { title: "Integrate" });
  return (
    <>
      <button type="button" className="btn btn--primary btn--sm" disabled={pending || mode !== "on"} onClick={() => setOpen(true)}>Integrate</button>
      <Dialog open={open} onClose={() => setOpen(false)} title={`Integrate “${ctx.title(entry.taskId)}”?`}>
        <p className="dialog__text">
          Brain re-checks the run against the current base and the decisions and rules it started with. If either changed, the entry is marked
          needs rebase or stale instructions instead, and the verdict stays on record. You can&apos;t integrate a run you started or requested.
        </p>
        <label className="field">
          <span className="field__label">Current base <em>optional, 40-character SHA</em></span>
          <input className="input input--mono" value={base} onChange={(e) => setBase(e.target.value)} placeholder="Defaults to the last integrated head" maxLength={40} />
          <span className="field__hint">Required until something has been integrated in this repository.</span>
        </label>
        <KnownShas entry={entry} known={known} onUse={setBase} />
        <div className="dialog__actions">
          <button type="button" className="btn btn--neutral" onClick={() => setOpen(false)}>Cancel</button>
          <button
            type="button"
            className="btn btn--primary"
            disabled={pending || mode !== "on"}
            onClick={async () => {
              const res = await run(ctx.board.id, entry.id, entry.version, base || undefined);
              if (res.ok) setOpen(false);
            }}
          >
            Integrate
          </button>
        </div>
      </Dialog>
    </>
  );
}

/** The SHAs a reviewer would otherwise hunt for: the base the board knows and the one the run started from. */
function KnownShas({ entry, known, onUse }: { entry: QueueEntry; known: ReturnType<typeof knownBase>; onUse: (sha: string) => void }) {
  const rows: [string, string][] = [];
  if (known) rows.push([known.source === "integrated" ? "Last integrated head" : "Base last checked", known.sha]);
  if (entry.baseSha && entry.baseSha !== known?.sha) rows.push(["Run started from", entry.baseSha]);
  if (!rows.length) return null;
  return (
    <dl className="board-shas">
      {rows.map(([label, sha]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>
            <code className="mono" title={sha}>{short(sha)}</code>
            <button type="button" className="btn btn--ghost btn--sm" onClick={() => onUse(sha)}>Use</button>
            <button type="button" className="btn btn--ghost btn--sm" onClick={() => navigator.clipboard?.writeText(sha).catch(() => undefined)}>Copy</button>
          </dd>
        </div>
      ))}
    </dl>
  );
}

function RejectButton({ entry, ctx, withdraw }: { entry: QueueEntry; ctx: Ctx; withdraw: boolean }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState(withdraw ? "Withdrawn by the member who queued it" : "");
  const { run, pending, mode } = useAction(rejectEntry, { title: withdraw ? "Withdraw" : "Reject" });
  const verb = withdraw ? "Withdraw" : "Reject";
  return (
    <>
      <button type="button" className="btn btn--neutral btn--sm" disabled={pending || mode !== "on"} onClick={() => setOpen(true)}>{verb}</button>
      <Dialog open={open} onClose={() => setOpen(false)} title={`${verb} “${ctx.title(entry.taskId)}” from the queue?`}>
        <label className="field">
          <span className="field__label">Reason</span>
          <textarea className="input" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={255} rows={3} />
          <span className="field__hint">Kept with the entry; the next entry in the queue can then be integrated.</span>
        </label>
        <div className="dialog__actions">
          <button type="button" className="btn btn--neutral" onClick={() => setOpen(false)}>Cancel</button>
          <button
            type="button"
            className="btn btn--danger"
            disabled={pending || mode !== "on" || !reason.trim()}
            onClick={async () => {
              const res = await run(ctx.board.id, entry.id, entry.version, reason);
              if (res.ok) setOpen(false);
            }}
          >
            {verb}
          </button>
        </div>
      </Dialog>
    </>
  );
}

function EntryControls({ entry, ctx, task }: { entry: QueueEntry; ctx: Ctx; task?: BoardTask }) {
  const can = entryActions(entry, ctx.board.permissions, ctx.selfId);
  const rerun = can.rerun && task && entry.status !== "queued";
  if (!can.integrate && !can.reject && !can.withdraw && !rerun) return null;
  return (
    <div className="row-actions">
      {can.integrate && <IntegrateButton entry={entry} ctx={ctx} />}
      {(can.reject || can.withdraw) && <RejectButton entry={entry} ctx={ctx} withdraw={can.withdraw} />}
      {rerun && task && <RequestRunButton task={task} ctx={ctx} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Conflict view

function Superseded({ items }: { items: SupersededInstruction[] }) {
  return (
    <ul className="board-list">
      {items.map((s) => (
        <li key={`${s.kind}-${s.id}`}>
          <a href={instructionHref(s)}>{s.kind === "rule" ? "Rule" : "Decision"} {s.id}</a>{" "}
          <span className="text-faint">
            {s.reason === "edited"
              ? `edited: the run has version ${s.version ?? "?"}, current is ${s.currentVersion ?? "newer"}`
              : `${s.reason} since the run started (it had version ${s.version ?? "?"})`}
          </span>
        </li>
      ))}
    </ul>
  );
}

function OverlapDetail({ o, ctx }: { o: Overlap; ctx: Ctx }) {
  return (
    <>
      <p className="board-conflict__who">
        {o.kind === "task" ? (
          <>Declared scope overlaps the active task <TaskLink id={o.otherTaskId ?? ""} ctx={ctx} fallback={o.otherTitle} />.</>
        ) : o.kind === "dependency" ? (
          <>Declared scope shares no path with the active task <TaskLink id={o.otherTaskId ?? ""} ctx={ctx} fallback={o.otherTitle} />, but imports or is imported by it.</>
        ) : (
          <>Path <span className="mono">{o.resource}</span> is leased by {who(o.holderId, ctx.selfId)} until {when(o.expiresAt)} (lease {o.leaseId}).</>
        )}
      </p>
      <ul className="board-list">
        {o.paths.map(([mine, theirs]) => (
          <li key={`${mine}|${theirs}`}><span className="mono">{mine}</span> <span className="text-faint">{o.kind === "dependency" ? "imports or is imported by" : "overlaps"}</span> <span className="mono">{theirs}</span></li>
        ))}
      </ul>
    </>
  );
}

/** What conflicts for one task, and what the member may do about it. */
export function ConflictView({ task, ctx }: { task: BoardTask; ctx: Ctx }) {
  const { board, selfId } = ctx;
  const overlaps = board.overlaps.filter((o) => o.taskId === task.id || o.otherTaskId === task.id);
  const entry = task.entry && (task.entry.status === "needs_rebase" || task.entry.status === "stale_instructions") ? task.entry : null;
  const instructions = entry ? board.instructions[entry.runId] : undefined;
  const myLeases = board.leases.filter((l) => leaseActions(l, board.permissions, selfId).release
    && (l.taskId === task.id || overlaps.some((o) => o.kind === "lease" && o.leaseId === l.id)));
  const can = entry ? entryActions(entry, board.permissions, selfId) : null;
  const hasAction = myLeases.length > 0 || Boolean(can && (can.integrate || can.reject || can.withdraw || can.rerun));
  return (
    <div className="board-conflict">
      {entry && (
        <section>
          <h3 className="board-conflict__head">
            <Chip tone="warn">{STATUS_WORD[entry.status]}</Chip> Queue entry {entry.id}
          </h3>
          {entry.status === "needs_rebase" ? (
            <p>
              The run started from <span className="mono">{short(entry.baseSha)}</span>, but the current base is{" "}
              <span className="mono">{short(entry.checkedBaseSha)}</span> (checked {when(entry.checkedAt)}). Rebase onto the current base and queue a new run.
            </p>
          ) : (
            <p>Decisions or rules the run started with changed since (checked {when(entry.checkedAt)}). A fresh run resolves the current versions.</p>
          )}
          {entry.reason && <p className="text-faint">{entry.reason}</p>}
          {instructions && instructions.superseded.length > 0 && <Superseded items={instructions.superseded} />}
          {entry.status === "stale_instructions" && !instructions && <p className="text-faint">Brain didn&apos;t return the run&apos;s instruction versions; open Memory to compare.</p>}
        </section>
      )}
      {overlaps.length > 0 && (
        <section>
          <h3 className="board-conflict__head"><Chip tone="warn">overlap</Chip> {overlaps.length === 1 ? "One overlap" : `${overlaps.length} overlaps`}</h3>
          {overlaps.map((o, i) => <OverlapDetail key={i} o={o.taskId === task.id ? o : { ...o, otherTaskId: o.taskId, otherTitle: ctx.title(o.taskId), paths: o.paths.map(([a, b]) => [b, a]) }} ctx={ctx} />)}
        </section>
      )}
      {!entry && overlaps.length === 0 && <p>Nothing conflicts with this task right now.</p>}
      <section>
        <h3 className="board-conflict__head">What you can do</h3>
        <div className="row-actions row-actions--wrap">
          {myLeases.map((l) => <ReleaseButton key={l.id} lease={l} ctx={ctx} />)}
          {entry && <EntryControls entry={entry} ctx={ctx} task={task} />}
        </div>
        {!hasAction && (
          <p className="text-faint">
            {overlaps.length && !entry
              ? "Overlaps are warnings: agree with the other holder who goes first, or narrow the declared scope. Nothing here is yours to release."
              : "Your role here can't change this; a contributor or reviewer resolves it."}
          </p>
        )}
        <LockNote />
      </section>
    </div>
  );
}

function ConflictButton({ task, ctx, label = "See conflict", className = "btn btn--ghost btn--sm" }: { task: BoardTask; ctx: Ctx; label?: string; className?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className={className} onClick={() => setOpen(true)}>{label}</button>
      <Dialog open={open} onClose={() => setOpen(false)} title={`Conflict: ${task.title}`} wide>
        <ConflictView task={task} ctx={ctx} />
      </Dialog>
    </>
  );
}

// ---------------------------------------------------------------------------
// Board

// Columns run left to right as work moves; a column's tone says whether it needs a person.
type StageTone = Tone | "teal";
const STAGE_TONE: Record<BoardColumn, StageTone> = {
  blocked: "idle", ready: "info", running: "teal", awaiting: "info", needs_rebase: "warn", stale_instructions: "warn", integrated: "ok",
};
const STAGE_SHORT: Record<BoardColumn, string> = {
  blocked: "Blocked", ready: "Ready", running: "Running", awaiting: "To integrate", needs_rebase: "Needs rebase", stale_instructions: "Stale", integrated: "Done",
};

// A card says only what a reviewer acts on: what it waits for, its queue place, a conflict.
// Scope paths sit in the tooltip; leases and overlaps in full are in the panels below.
function Card({ task, ctx, position }: { task: BoardTask; ctx: Ctx; position: number | null }) {
  const overlaps = ctx.board.overlaps.filter((o) => o.taskId === task.id || o.otherTaskId === task.id).length;
  const conflicted = overlaps > 0 || task.column === "needs_rebase" || task.column === "stale_instructions";
  const waiting = task.waitingOn;
  return (
    <li className={`kcard${conflicted ? " kcard--warn" : ""}`} id={anchor(task.id)} title={task.scopePaths.length ? `Scope:\n${task.scopePaths.join("\n")}` : undefined}>
      <span className="kcard__title">{task.title}</span>
      {task.parentId && <span className="kcard__sub">in <TaskLink id={task.parentId} ctx={ctx} /></span>}
      {waiting.length > 0 && (
        <p className="kcard__wait">
          <span>Waiting on</span>{" "}
          {waiting.map((d, i) => <span key={d}>{i > 0 && ", "}<TaskLink id={d} ctx={ctx} /></span>)}
        </p>
      )}
      {(position !== null || conflicted) && (
        <div className="kcard__foot">
          {position !== null && <span className="kpill tone-info" title="Position in the integration queue">Queue #{position}</span>}
          {conflicted && (
            <ConflictButton task={task} ctx={ctx} className="kpill kpill--btn tone-warn"
              label={overlaps > 1 ? `${overlaps} overlaps` : overlaps === 1 ? "Overlap" : "Conflict"} />
          )}
        </div>
      )}
    </li>
  );
}

export function BoardColumns({ board, selfId }: { board: ProjectBoard; selfId: number | null }) {
  const ctx = context(board, selfId);
  const position = new Map(board.queue.map((e, i) => [e.taskId, i + 1]));
  if (!board.tasks.length) return <p className="kanban__empty">No tasks in this project yet.</p>;
  return (
    <div className="kanban" aria-label={`Tasks in ${board.name} by stage`}>
      {BOARD_COLUMNS.map((col) => {
        const tasks = board.tasks.filter((t) => t.column === col.id);
        return (
          <section key={col.id} className={`kcol kcol--${col.id} tone-${STAGE_TONE[col.id]}`} aria-label={col.label}>
            <h3 className="kcol__head" title={`${col.label}: ${col.hint}`}>
              <span className="dot" aria-hidden />
              {STAGE_SHORT[col.id]}
              <span className="kcol__count num">{tasks.length}</span>
            </h3>
            {tasks.length ? (
              <ul className="kcol__cards">
                {tasks.map((t) => <Card key={t.id} task={t} ctx={ctx} position={position.get(t.id) ?? null} />)}
              </ul>
            ) : (
              <p className="kcol__none">No tasks</p>
            )}
          </section>
        );
      })}
    </div>
  );
}

function context(board: ProjectBoard, selfId: number | null): Ctx {
  const titles = new Map(board.tasks.map((t) => [t.id, t.title]));
  return { board, selfId, title: (id) => titles.get(id) ?? `task ${id.slice(0, 8)}`, has: (id) => titles.has(id) };
}

export function LeasesTable({ board, selfId }: { board: ProjectBoard; selfId: number | null }) {
  const ctx = context(board, selfId);
  return (
    <>
      <Table<Lease>
        caption="Live leases"
        rows={board.leases}
        rowKey={(l) => String(l.id)}
        columns={[
          { head: "Path", cell: (l) => <span className="mono">{l.resource}</span> },
          { head: "Holder", cell: (l) => who(l.holderId, selfId) },
          { head: "Task", cell: (l) => (l.taskId ? <TaskLink id={l.taskId} ctx={ctx} /> : <span className="text-faint">none</span>) },
          { head: "Expires", cell: (l) => <><span className="num">{when(l.expiresAt)}</span><br /><span className="text-faint num" title="Fencing token: Brain refuses writes made under an older one">token {l.fencingToken}</span></> },
          { head: "", cell: (l) => <LeaseControls lease={l} ctx={ctx} /> },
        ]}
      />
      <LockNote />
    </>
  );
}

export function OverlapList({ board, selfId }: { board: ProjectBoard; selfId: number | null }) {
  const ctx = context(board, selfId);
  const byId = new Map(board.tasks.map((t) => [t.id, t]));
  return (
    <ul className="board-overlaps">
      {board.overlaps.map((o, i) => {
        const task = byId.get(o.taskId);
        return (
          <li key={i} className="board-overlap">
            <div>
              <b><TaskLink id={o.taskId} ctx={ctx} /></b>
              <OverlapDetail o={o} ctx={ctx} />
            </div>
            {task && <ConflictButton task={task} ctx={ctx} label="What can I do?" />}
          </li>
        );
      })}
    </ul>
  );
}

export function QueueTable({ board, selfId }: { board: ProjectBoard; selfId: number | null }) {
  const ctx = context(board, selfId);
  const byId = new Map(board.tasks.map((t) => [t.id, t]));
  return (
    <>
      <Table<QueueEntry>
        caption="Pending integration entries"
        rows={board.queue}
        rowKey={(e) => String(e.id)}
        columns={[
          { head: "Repo queue", cell: (e) => <span className="num">{queuePosition(board.queue, e) ?? "—"}</span> },
          { head: "Task", cell: (e) => <><TaskLink id={e.taskId} ctx={ctx} /><span className="sub mono">run {e.runId.slice(0, 8)} · head {short(e.headSha)}</span></> },
          { head: "Status", cell: (e) => <Chip tone={STATUS_TONE[e.status]}>{STATUS_WORD[e.status]}</Chip> },
          { head: "Base", cell: (e) => <span className="mono">{short(e.baseSha)}{e.checkedBaseSha && e.checkedBaseSha !== e.baseSha ? ` → ${short(e.checkedBaseSha)}` : ""}</span> },
          { head: "Queued by", cell: (e) => who(e.enqueuedBy, selfId) },
          {
            head: "",
            cell: (e) => {
              const task = byId.get(e.taskId);
              return (
                <div className="row-actions">
                  {task && e.status !== "queued" && <ConflictButton task={task} ctx={ctx} />}
                  <EntryControls entry={e} ctx={ctx} />
                </div>
              );
            },
          },
        ]}
      />
      <LockNote />
    </>
  );
}
