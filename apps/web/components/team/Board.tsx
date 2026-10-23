"use client";
// The team Board: a sentence on what can't land and what is next, the plan by
// stage, who is changing which path, the integration queue, and a side column
// with what needs a person or one task in full. Controls appear only where
// Brain would allow them (lib/board-model.ts); Brain still checks every call
// with the member's own key, the lease's fencing token and the entry's version.

import { createContext, useContext, useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { CodeBlock, Dialog, LockNote, RunButton, useAction } from "@/components/act";
import { AtlasVerdict, reveal } from "@/components/atlas/Atlas";
import { Chip, EmptyState } from "@/components/ui";
import { integrateEntry, rejectEntry, releaseLease, renewLease, requestRun, type NewRun } from "@/lib/actions/board";
import {
  BOARD_COLUMNS, attention, boardVerdict, claimMap, entryActions, instructionHref, knownBase, leaseActions, normPath, queuePosition,
  type Attention, type BoardColumn, type BoardTask, type Claim, type Lease, type Overlap, type ProjectBoard, type QueueEntry, type QueueStatus,
  type SupersededInstruction,
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

function IntegrateButton({ entry, ctx, label = "Integrate", small = true }: { entry: QueueEntry; ctx: Ctx; label?: string; small?: boolean }) {
  const known = knownBase(ctx.board, entry);
  const [open, setOpen] = useState(false);
  const [base, setBase] = useState(known?.source === "integrated" ? known.sha : "");
  const { run, pending, mode } = useAction(integrateEntry, { title: "Integrate" });
  return (
    <>
      <button type="button" className={`btn btn--primary${small ? " btn--sm" : ""}`} disabled={pending || mode !== "on"} onClick={() => setOpen(true)}>{label}</button>
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
// Board: a sentence, the plan by stage, who changes which path, the queue, and
// a side column that lists what needs a person or inspects one task.

// Stages run left to right as work moves; a stage's tone says whether it needs a person.
type StageTone = Tone | "teal";
const STAGE_TONE: Record<BoardColumn, StageTone> = {
  blocked: "idle", ready: "info", running: "teal", awaiting: "info", needs_rebase: "warn", stale_instructions: "warn", integrated: "ok",
};
const STAGE_SHORT: Record<BoardColumn, string> = {
  blocked: "Blocked", ready: "Ready", running: "Running", awaiting: "To integrate", needs_rebase: "Needs rebase", stale_instructions: "Stale", integrated: "Done",
};
const STAGE_INDEX = new Map(BOARD_COLUMNS.map((c, i) => [c.id, i]));
const REFRESH_MS = 30_000;

function context(board: ProjectBoard, selfId: number | null): Ctx {
  const titles = new Map(board.tasks.map((t) => [t.id, t.title]));
  return { board, selfId, title: (id) => titles.get(id) ?? `task ${id.slice(0, 8)}`, has: (id) => titles.has(id) };
}

/** Which tasks are lit (hovered anywhere on the board) and how to inspect one. */
interface Focus { lit: string[] | null; picked: string | null; light: (ids: string[] | null) => void; pick: (id: string | null) => void }
const FocusContext = createContext<Focus>({ lit: null, picked: null, light: () => undefined, pick: () => undefined });
/** data-lit / data-dim for an element showing these tasks, while something is lit. */
function useLit(ids: string[]) {
  const { lit, picked } = useContext(FocusContext);
  if (lit === null) return {};
  const hit = ids.some((id) => lit.includes(id) || id === picked);
  return { "data-lit": hit || undefined, "data-dim": !hit || undefined };
}

// The demo runs from its own moment, so its leases count down instead of reading "expired".
const ClockContext = createContext<number | null>(null);
function useNow(): number | null {
  const anchor = useContext(ClockContext);
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    const start = Date.now();
    const tick = () => setNow(anchor === null ? Date.now() : anchor + Date.now() - start);
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [anchor]);
  return now;
}

/** Time left on a lease, by the second once the page runs; the server renders the end time instead. */
function Left({ at }: { at: string | null }) {
  const now = useNow();
  if (!at) return <span className="text-faint">no expiry</span>;
  if (now === null) return <span className="tb-left num">until {when(at).slice(11)}</span>;
  const s = Math.round((Date.parse(at) - now) / 1000);
  if (s <= 0) return <span className="tb-left tone-bad num" title={`Expired ${when(at)}`}>expired</span>;
  return (
    <span className={`tb-left num${s < 120 ? " tone-warn" : ""}`} title={`Expires ${when(at)}`}>
      {Math.floor(s / 60)}m {String(s % 60).padStart(2, "0")}s left
    </span>
  );
}

const Lock = () => (
  <svg className="tb-lock" viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
    <rect x="2" y="5.5" width="8" height="5.5" rx="1.2" fill="currentColor" />
    <path d="M4 5.5V4a2 2 0 0 1 4 0v1.5" fill="none" stroke="currentColor" strokeWidth="1.4" />
  </svg>
);

/** A task named in running text: hover lights it everywhere, click inspects it. */
function TaskRef({ id, ctx, fallback }: { id: string; ctx: Ctx; fallback?: string | null }) {
  const f = useContext(FocusContext);
  const label = ctx.has(id) ? ctx.title(id) : fallback || ctx.title(id);
  if (!ctx.has(id)) return <span>{label}</span>;
  return (
    <button type="button" className="tb-ref" onClick={() => f.pick(id)} onMouseEnter={() => f.light([id])} onMouseLeave={() => f.light(null)}>
      {label}
    </button>
  );
}

function Stage({ column }: { column: BoardColumn }) {
  return <span className={`tb-stage tone-${STAGE_TONE[column]}`}><span className="dot" aria-hidden="true" />{STAGE_SHORT[column]}</span>;
}

function Section({ title, note, children }: { title: string; note?: ReactNode; children: ReactNode }) {
  return (
    <section className="tb-sec">
      <header className="tb-sec__head">
        <h3>{title}</h3>
        {note && <span className="tb-sec__note">{note}</span>}
      </header>
      {children}
    </section>
  );
}

// ---------- plan: every task in flight, one row each, its marker in the stage it has reached

function planRows(board: ProjectBoard) {
  const shown = board.tasks.filter((t) => t.column !== null);
  const ids = new Set(shown.map((t) => t.id));
  const children = (id: string) => shown.filter((t) => t.parentId === id);
  const rows: { task: BoardTask; kids: BoardTask[]; child: boolean }[] = [];
  for (const t of shown) {
    if (t.parentId && ids.has(t.parentId)) continue;
    const kids = children(t.id);
    rows.push({ task: t, kids, child: false });
    for (const k of kids) rows.push({ task: k, kids: [], child: true });
  }
  return rows;
}

function PlanRow({ task, kids, child, ctx, position }: { task: BoardTask; kids: BoardTask[]; child: boolean; ctx: Ctx; position: number | null }) {
  const f = useContext(FocusContext);
  const column = task.column as BoardColumn;
  const at = STAGE_INDEX.get(column) ?? 0;
  const overlaps = ctx.board.overlaps.filter((o) => o.taskId === task.id || o.otherTaskId === task.id).length;
  const conflicted = overlaps > 0 || column === "needs_rebase" || column === "stale_instructions";
  const lit = useLit([task.id, ...kids.map((k) => k.id)]);
  const done = kids.filter((k) => k.column === "integrated").length;
  return (
    <li
      id={anchor(task.id)}
      className={`tb-row tone-${STAGE_TONE[column]}${child ? " tb-row--child" : ""}${kids.length ? " tb-row--group" : ""}`}
      data-picked={f.picked === task.id || undefined}
      {...lit}
    >
      <button
        type="button"
        className="tb-row__hit"
        onClick={() => f.pick(task.id)}
        onMouseEnter={() => f.light([task.id])}
        onMouseLeave={() => f.light(null)}
        onFocus={() => f.light([task.id])}
        onBlur={() => f.light(null)}
        aria-label={`${task.title}: ${STAGE_SHORT[column]}`}
      />
      <div className="tb-row__title">
        <span className="tb-row__name">{task.title}</span>
        <span className="tb-row__sub">
          {kids.length ? (
            <span>{done} of {kids.length} subtasks done</span>
          ) : task.waitingOn.length ? (
            <span className="tb-row__wait">waits for {task.waitingOn.map((d) => ctx.title(d)).join(", ")}</span>
          ) : task.scopePaths.length ? (
            <span className="mono">{task.scopePaths[0]}{task.scopePaths.length > 1 ? ` +${task.scopePaths.length - 1}` : ""}</span>
          ) : null}
          {conflicted && <span className="tb-row__flag">{overlaps > 1 ? `${overlaps} overlaps` : overlaps === 1 ? "overlap" : "conflict"}</span>}
        </span>
      </div>
      <span className="tb-row__inline"><Stage column={column} /></span>
      {kids.length ? (
        BOARD_COLUMNS.map((c) => {
          const n = kids.filter((k) => k.column === c.id).length;
          return (
            <span key={c.id} className={`tb-cell tone-${STAGE_TONE[c.id]}`} aria-hidden="true">
              {n > 0 && <span className="tb-tally">{Array.from({ length: Math.min(n, 5) }, (_, i) => <i key={i} />)}{n > 5 && <b>+{n - 5}</b>}</span>}
            </span>
          );
        })
      ) : (
        BOARD_COLUMNS.map((c, i) => (
          <span key={c.id} className={`tb-cell${i > 0 && i < at ? " tb-cell--trail" : ""}${i === 1 && at > 1 ? " tb-cell--from" : ""}${i === at ? " tb-cell--at" : ""}${i === at && at > 1 ? " tb-cell--arrive" : ""}`} aria-hidden="true">
            {i === at && (
              <span className={`tb-marker${conflicted ? " tb-marker--warn" : ""}`}>{position !== null ? `#${position}` : column === "integrated" ? "✓" : ""}</span>
            )}
          </span>
        ))
      )}
    </li>
  );
}

function Plan({ ctx }: { ctx: Ctx }) {
  const { board } = ctx;
  const rows = planRows(board);
  if (!rows.length) return <EmptyState title="No tasks yet" body="Tasks and subtasks created for this project show up here, each in the stage it has reached." />;
  const count = (c: BoardColumn) => board.tasks.filter((t) => t.column === c && !board.tasks.some((k) => k.parentId === t.id)).length;
  return (
    <div className="tb-plan" role="group" aria-label={`Tasks in ${board.name} by stage`}>
      <div className="tb-plan__head" aria-hidden="true">
        <span />
        {BOARD_COLUMNS.map((c) => {
          const n = count(c.id);
          return (
            <span key={c.id} className={`tb-plan__stage tone-${STAGE_TONE[c.id]}`} data-empty={n === 0 || undefined} title={`${c.label}: ${c.hint}`}>
              <b className="num">{n}</b>{STAGE_SHORT[c.id]}
            </span>
          );
        })}
      </div>
      <ol className="tb-plan__rows">
        {rows.map((r) => (
          <PlanRow key={r.task.id} {...r} ctx={ctx} position={r.task.entry && r.task.entry.status === "queued" ? queuePosition(board.queue, r.task.entry) : null} />
        ))}
      </ol>
    </div>
  );
}

// ---------- claims: which paths are being changed, by which task, under which lease

function Claims({ ctx }: { ctx: Ctx }) {
  const { board, selfId } = ctx;
  const f = useContext(FocusContext);
  const claims = claimMap(board);
  const byId = new Map(board.tasks.map((t) => [t.id, t]));
  if (!claims.length) return <p className="tb-none">No task in flight declares a path, and nobody holds a lease.</p>;
  return (
    <ul className="tb-claims">
      {claims.map((c) => <ClaimRow key={c.path} c={c} ctx={ctx} byId={byId} selfId={selfId} f={f} />)}
    </ul>
  );
}

function ClaimRow({ c, ctx, byId, selfId, f }: { c: Claim; ctx: Ctx; byId: Map<string, BoardTask>; selfId: number | null; f: Focus }) {
  const ids = [...c.tasks, ...c.leases.map((l) => l.taskId).filter((id): id is string => id !== null)];
  const lit = useLit(ids);
  return (
    <li
      className={`tb-claim${c.overlap ? " tb-claim--shared" : ""}`}
      style={{ "--depth": c.depth } as CSSProperties}
      onMouseEnter={() => ids.length && f.light(ids)}
      onMouseLeave={() => f.light(null)}
      title={c.path}
      {...lit}
    >
      <span className="tb-claim__path mono">
        {c.depth > 0 && <span className="tb-claim__elbow" aria-hidden="true" />}
        {c.label}
      </span>
      <span className="tb-claim__who">
        {c.overlap && <span className="tb-claim__shared">shared</span>}
        {c.tasks.map((id) => {
          const t = byId.get(id);
          return (
            <button key={id} type="button" className={`tb-chip tone-${t?.column ? STAGE_TONE[t.column] : "idle"}`} onClick={() => f.pick(id)}>
              <span className="dot" aria-hidden="true" />{ctx.title(id)}
            </button>
          );
        })}
        {c.leases.map((l) => (
          <span key={l.id} className={`tb-chip tb-chip--lease${l.holderId === selfId ? " tone-teal" : " tone-idle"}`} title={`Lease ${l.id}, fencing token ${l.fencingToken}: writes under an older token are refused`}>
            <Lock />{who(l.holderId, selfId)} · <Left at={l.expiresAt} />
          </span>
        ))}
      </span>
    </li>
  );
}

// ---------- queue: per repository, the last integrated head, then the line in order

function Queue({ ctx }: { ctx: Ctx }) {
  const { board, selfId } = ctx;
  const byId = new Map(board.tasks.map((t) => [t.id, t]));
  const repoIds = [...new Set([...board.queue.map((e) => e.repositoryId), ...board.tasks.filter((t) => t.entry?.status === "integrated").map((t) => t.repositoryId)])];
  if (!board.queue.length) return <p className="tb-none">The queue is empty. Runs queued for integration line up here, oldest first.</p>;
  const repoName = (id: number | null) => board.repositories.find((r) => r.id === id)?.name ?? "repository";
  return (
    <div className="tb-queues">
      {repoIds.map((repo) => {
        const line = board.queue.filter((e) => e.repositoryId === repo && e.status === "queued");
        const held = board.queue.filter((e) => e.repositoryId === repo && e.status !== "queued");
        if (!line.length && !held.length) return null;
        const last = board.tasks.map((t) => t.entry).filter((e): e is QueueEntry => e !== null && e.status === "integrated" && e.repositoryId === repo)
          .sort((a, b) => (Date.parse(b.decidedAt ?? "") || 0) - (Date.parse(a.decidedAt ?? "") || 0) || b.id - a.id)[0];
        return (
          <div key={String(repo)} className="tb-queue">
            {repoIds.length > 1 && <p className="tb-queue__repo mono">{repoName(repo)}</p>}
            <ol className="tb-train">
              <li className="tb-train__base">
                <span className="tb-train__pos" aria-hidden="true">✓</span>
                <span className="tb-train__body">
                  <span className="tb-train__label">Base</span>
                  {last ? <span>last integrated <TaskRef id={last.taskId} ctx={ctx} /> · head <code className="mono" title={last.headSha}>{short(last.headSha)}</code></span> : <span className="text-faint">nothing integrated here yet</span>}
                </span>
              </li>
              {line.map((e, i) => (
                <TrainCar key={e.id} entry={e} pos={i + 1} ctx={ctx} task={byId.get(e.taskId)} selfId={selfId} />
              ))}
              {!line.length && <li className="tb-train__empty text-faint">Nothing is waiting in line.</li>}
            </ol>
            {held.length > 0 && (
              <div className="tb-held">
                <p className="tb-held__head">Held back <span className="text-faint">— they don’t block the line</span></p>
                <ul>
                  {held.map((e) => {
                    const task = byId.get(e.taskId);
                    return (
                      <HeldRow key={e.id} entry={e} ctx={ctx} task={task} />
                    );
                  })}
                </ul>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function TrainCar({ entry, pos, ctx, task, selfId }: { entry: QueueEntry; pos: number; ctx: Ctx; task?: BoardTask; selfId: number | null }) {
  const lit = useLit([entry.taskId]);
  return (
    <li className={`tb-train__car${pos === 1 ? " tb-train__car--head" : ""}`} {...lit}>
      <span className="tb-train__pos num">{pos}</span>
      <span className="tb-train__body">
        <TaskRef id={entry.taskId} ctx={ctx} />
        <span className="tb-train__meta">
          queued by {who(entry.enqueuedBy, selfId)}{entry.enqueuedAt ? ` · ${when(entry.enqueuedAt).slice(11)}` : ""} · head <code className="mono" title={entry.headSha}>{short(entry.headSha)}</code>
        </span>
      </span>
      <EntryControls entry={entry} ctx={ctx} task={task} />
    </li>
  );
}

function HeldRow({ entry, ctx, task }: { entry: QueueEntry; ctx: Ctx; task?: BoardTask }) {
  const lit = useLit([entry.taskId]);
  return (
    <li className="tb-held__row" {...lit}>
      <span className="tb-held__body">
        <span><TaskRef id={entry.taskId} ctx={ctx} /> <Chip tone={STATUS_TONE[entry.status]}>{STATUS_WORD[entry.status]}</Chip></span>
        {entry.reason && <span className="tb-held__why">{entry.reason}</span>}
      </span>
      {task && <ConflictButton task={task} ctx={ctx} label="Resolve" className="btn btn--neutral btn--sm" />}
    </li>
  );
}

// ---------- side column: what needs a person, or one task in full

function NeedsYou({ ctx, items }: { ctx: Ctx; items: Attention[] }) {
  const { board, selfId } = ctx;
  const byId = new Map(board.tasks.map((t) => [t.id, t]));
  if (!items.length) {
    return (
      <div className="tb-needs__none">
        <p className="tb-needs__calm">Nothing needs a person.</p>
        <p className="text-faint">Runs that can’t land, entries waiting for your review, your leases and overlapping work show up here first.</p>
      </div>
    );
  }
  return (
    <ul className="tb-needs">
      {items.map((a) => {
        const task = a.taskId ? byId.get(a.taskId) : undefined;
        const key = a.kind === "lease" ? `lease-${a.lease.id}` : a.kind === "overlap" ? `overlap-${a.taskId}-${a.overlap.leaseId ?? a.overlap.otherTaskId}` : `${a.kind}-${a.entry.id}`;
        return <NeedItem key={key} a={a} task={task} ctx={ctx} selfId={selfId} />;
      })}
    </ul>
  );
}

function NeedItem({ a, task, ctx, selfId }: { a: Attention; task?: BoardTask; ctx: Ctx; selfId: number | null }) {
  const f = useContext(FocusContext);
  const ids = [a.taskId, a.kind === "overlap" ? a.overlap.otherTaskId : null].filter((id): id is string => Boolean(id));
  const tone = a.kind === "held" || a.kind === "overlap" ? "warn" : a.kind === "integrate" ? "info" : "teal";
  return (
    <li className={`tb-need tone-${tone}`} onMouseEnter={() => f.light(ids)} onMouseLeave={() => f.light(null)}>
      <p className="tb-need__what">
        {a.kind === "held" && <><TaskRef id={a.taskId} ctx={ctx} /> {a.entry.status === "needs_rebase" ? "needs a rebase" : "has stale instructions"}</>}
        {a.kind === "integrate" && <><TaskRef id={a.taskId} ctx={ctx} /> is first in line for your review</>}
        {a.kind === "lease" && <>Your lease on <span className="mono">{a.lease.resource}</span></>}
        {a.kind === "overlap" && (a.overlap.kind === "lease"
          ? <><TaskRef id={a.taskId} ctx={ctx} /> needs <span className="mono">{a.overlap.resource}</span>, leased by {who(a.overlap.holderId, selfId)}</>
          : <><TaskRef id={a.taskId} ctx={ctx} /> {a.overlap.kind === "dependency" ? "depends on code in" : "overlaps"} <TaskRef id={a.overlap.otherTaskId ?? ""} ctx={ctx} fallback={a.overlap.otherTitle} /></>)}
      </p>
      <p className="tb-need__why">
        {a.kind === "held" && (a.entry.status === "needs_rebase"
          ? <>Started from <span className="mono">{short(a.entry.baseSha)}</span>; the base is now <span className="mono">{short(a.entry.checkedBaseSha)}</span>.</>
          : "A decision or rule changed since the run started.")}
        {a.kind === "integrate" && <>Queued by {who(a.entry.enqueuedBy, selfId)} · head <span className="mono">{short(a.entry.headSha)}</span></>}
        {a.kind === "lease" && <><Left at={a.lease.expiresAt} /> · token {a.lease.fencingToken}{a.lease.taskId ? <> · for <TaskRef id={a.lease.taskId} ctx={ctx} /></> : null}</>}
        {a.kind === "overlap" && (a.overlap.kind === "lease" ? <>The lease ends <Left at={a.overlap.expiresAt} />.</> : "A warning: nothing is blocked.")}
      </p>
      <div className="row-actions row-actions--wrap">
        {a.kind === "held" && task && <ConflictButton task={task} ctx={ctx} label="Resolve" className="btn btn--neutral btn--sm" />}
        {a.kind === "integrate" && <EntryControls entry={a.entry} ctx={ctx} task={task} />}
        {a.kind === "lease" && <LeaseControls lease={a.lease} ctx={ctx} />}
        {a.kind === "overlap" && task && <ConflictButton task={task} ctx={ctx} label="What can I do?" className="btn btn--ghost btn--sm" />}
      </div>
    </li>
  );
}

function Inspector({ task, ctx }: { task: BoardTask; ctx: Ctx }) {
  const { board, selfId } = ctx;
  const f = useContext(FocusContext);
  const column = task.column as BoardColumn;
  const hint = BOARD_COLUMNS.find((c) => c.id === column)?.hint;
  const kids = board.tasks.filter((t) => t.parentId === task.id && t.column !== null);
  const leases = board.leases.filter((l) => l.taskId === task.id);
  const hot = new Set(board.overlaps.filter((o) => o.taskId === task.id).flatMap((o) => o.paths.map(([mine]) => normPath(mine))));
  const conflicted = column === "needs_rebase" || column === "stale_instructions" || board.overlaps.some((o) => o.taskId === task.id || o.otherTaskId === task.id);
  const pending = task.entry && task.entry.status === "queued" ? task.entry : null;
  const canRun = column === "ready" && board.permissions.includes("run.request");
  return (
    <div className="tb-inspect">
      <button type="button" className="tb-back" onClick={() => f.pick(null)}>‹ What needs attention</button>
      <div className="tb-inspect__head">
        <Stage column={column} />
        {task.parentId && <span className="text-faint">in <TaskRef id={task.parentId} ctx={ctx} fallback={task.parentTitle} /></span>}
      </div>
      <h3 className="tb-inspect__title">{task.title}</h3>
      {hint && <p className="tb-inspect__hint">{hint}.</p>}
      <dl className="tb-facts">
        {kids.length > 0 && (
          <div><dt>Subtasks</dt><dd><ul className="tb-facts__list">{kids.map((k) => <li key={k.id}><TaskRef id={k.id} ctx={ctx} /> <Stage column={k.column as BoardColumn} /></li>)}</ul></dd></div>
        )}
        {task.waitingOn.length > 0 && (
          <div><dt>Waits for</dt><dd className="tb-facts__warn">{task.waitingOn.map((d, i) => <span key={d}>{i > 0 && ", "}<TaskRef id={d} ctx={ctx} /></span>)}</dd></div>
        )}
        {task.dependsOn.length > 0 && (
          <div><dt>Depends on</dt><dd>{task.dependsOn.map((d, i) => <span key={d}>{i > 0 && ", "}<TaskRef id={d} ctx={ctx} /></span>)}</dd></div>
        )}
        {task.scopePaths.length > 0 && (
          <div><dt>Changes</dt><dd><ul className="tb-facts__list">{task.scopePaths.map((p) => <li key={p} className="mono">{p}{hot.has(normPath(p)) && <span className="tb-claim__shared">shared</span>}</li>)}</ul></dd></div>
        )}
        {task.branch && <div><dt>Branch</dt><dd className="mono">{task.branch}</dd></div>}
        {task.runs.length > 0 && <div><dt>Runs</dt><dd><span className="num">{task.runs.length}</span> <span className="text-faint mono">latest {task.runs[task.runs.length - 1].slice(0, 8)}</span></dd></div>}
        {pending && <div><dt>Queue</dt><dd>#{queuePosition(board.queue, pending)} in line · queued by {who(pending.enqueuedBy, selfId)}</dd></div>}
        {leases.map((l) => (
          <div key={l.id}>
            <dt>Lease</dt>
            <dd className="tb-facts__lease">
              <span><Lock /> <span className="mono">{l.resource}</span> · {who(l.holderId, selfId)} · <Left at={l.expiresAt} /></span>
              <LeaseControls lease={l} ctx={ctx} />
            </dd>
          </div>
        ))}
      </dl>
      {(canRun || pending) && (
        <div className="row-actions row-actions--wrap">
          {canRun && <RequestRunButton task={task} ctx={ctx} />}
          {pending && <EntryControls entry={pending} ctx={ctx} task={task} />}
        </div>
      )}
      {conflicted && <ConflictView task={task} ctx={ctx} />}
    </div>
  );
}

/** One project's board. `clock` freezes "now" for the demo; `live` refreshes the data while the tab is visible. */
export function TeamBoard({ board, selfId, live, clock, named }: { board: ProjectBoard; selfId: number | null; live: boolean; clock: string | null; named: boolean }) {
  const ctx = useMemo(() => context(board, selfId), [board, selfId]);
  const router = useRouter();
  const [lit, setLit] = useState<string[] | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const asideId = `board-${board.id}-aside`;
  const focus = useMemo<Focus>(() => ({
    lit, picked, light: setLit,
    pick: (id) => {
      setPicked(id);
      setLit(null);
      requestAnimationFrame(() => reveal(document.getElementById(asideId)));
    },
  }), [lit, picked, asideId]);

  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => { if (document.visibilityState === "visible") router.refresh(); }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [live, router]);

  const segments = useMemo(() => boardVerdict(board, selfId), [board, selfId]);
  const items = useMemo(() => attention(board, selfId), [board, selfId]);
  const next = items.find((a): a is Extract<Attention, { kind: "integrate" }> => a.kind === "integrate");
  const task = picked ? board.tasks.find((t) => t.id === picked && t.column !== null) : undefined;
  const active = board.tasks.filter((t) => t.column !== null && t.column !== "integrated" && !board.tasks.some((k) => k.parentId === t.id)).length;
  const done = board.tasks.filter((t) => t.column === "integrated" && !board.tasks.some((k) => k.parentId === t.id)).length;
  const repos = board.repositories.map((r) => r.name).join(", ");
  const notes = [
    board.missing.length ? `Brain didn’t return ${board.missing.join(", ")}; the rest of the board is current.` : null,
    board.truncated ? "This project has more tasks than the board loads; the oldest are not shown." : null,
  ].filter((n): n is string => n !== null);
  const counts = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

  return (
    <ClockContext.Provider value={clock ? Date.parse(clock) : null}>
      <FocusContext.Provider value={focus}>
        <section className="tb" id={`board-${board.id}`} aria-label={`${board.name} board`}>
          {named && <h2 className="tb__project">{board.name}</h2>}
          {notes.map((n) => <div key={n} className="callout tone-warn">{n}</div>)}
          <AtlasVerdict
            segments={segments}
            sub={<>{counts(active, "task", "tasks")} in flight · {counts(board.queue.length, "entry", "entries")} in the queue · {counts(board.leases.length, "live lease", "live leases")} · you are <b>{board.role}</b>{repos ? ` on ${repos}` : ""}</>}
            actions={next ? <IntegrateButton entry={next.entry} ctx={ctx} label="Integrate next" small={false} /> : null}
            onMention={(ref) => setLit(ref ? [ref] : null)}
            onPick={(ref) => focus.pick(ref)}
          />
          <div className="atlas-split tb-split">
            <div className="tb-main">
              <Section title="Plan" note={`${active} in flight · ${done} done`}><Plan ctx={ctx} /></Section>
              <Section title="Who is changing what" note="declared scopes and live leases, by path"><Claims ctx={ctx} /></Section>
              <Section title="Integration queue" note="a reviewer lands entries in order; the first blocks the rest"><Queue ctx={ctx} /></Section>
            </div>
            <aside className="tb-aside" id={asideId} aria-label={task ? `Task: ${task.title}` : "What needs attention"}>
              {task ? (
                <Inspector key={task.id} task={task} ctx={ctx} />
              ) : (
                <>
                  <header className="tb-aside__head">
                    <h3>Needs attention <span className="tb-count num">{items.length}</span></h3>
                    {live && <span className="chip chip--info chip--live"><span className="chip__dot" aria-hidden="true" />Live</span>}
                  </header>
                  <NeedsYou ctx={ctx} items={items} />
                </>
              )}
              <LockNote />
            </aside>
          </div>
        </section>
      </FocusContext.Provider>
    </ClockContext.Provider>
  );
}

