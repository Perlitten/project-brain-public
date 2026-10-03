"use client";
// Everything that makes a control *do* something: who may act (mode), the
// toast stack that reports each outcome, background-job toasts that follow a
// job to its end, a run button with optional confirmation, and a copyable
// code block. Server actions come in as props (bound on the server) or are
// imported directly; results are always ActionResult, one sentence each.

import { useRouter } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  useTransition,
  type ReactNode,
} from "react";
import { getJob } from "@/lib/actions/jobs";
import type { ActionResult, ActionsMode, JobSnapshot } from "@/lib/actions/types";
import type { Tone } from "@/lib/types";
import { Icon, type IconName } from "./Icon";

// ---------------------------------------------------------------------------
// Who may act

const ModeContext = createContext<ActionsMode>("demo");

export function ActionsProvider({ mode, children }: { mode: ActionsMode; children: ReactNode }) {
  return <ModeContext.Provider value={mode}>{children}</ModeContext.Provider>;
}

export const useActionsMode = () => useContext(ModeContext);

export const LOCK_REASON: Record<ActionsMode, string> = {
  demo: "Connect the Brain API to run actions — this is demo data.",
  locked: "Actions are locked: protect the dashboard with WEB_BASIC_AUTH (or set BRAIN_WEB_ACTIONS=1).",
  on: "",
};

// ---------------------------------------------------------------------------
// Toasts

export interface Toast {
  id: number;
  tone: Tone;
  message: string;
  /** Follow this background job until it finishes. */
  job?: string;
  title?: string;
}

let toasts: Toast[] = [];
let seq = 0;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

export function toast(t: Omit<Toast, "id">): number {
  const id = ++seq;
  toasts = [...toasts.slice(-4), { ...t, id }];
  emit();
  return id;
}

export function dismissToast(id: number) {
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

function updateToast(id: number, patch: Partial<Toast>) {
  toasts = toasts.map((t) => (t.id === id ? { ...t, ...patch } : t));
  emit();
}

/** Report an action result: a job toast when it queued one, else a plain one. */
export function report(res: ActionResult, title?: string) {
  if (res.ok && res.jobId) return toast({ tone: "info", message: res.message, job: res.jobId, title });
  return toast({ tone: res.ok ? "ok" : "bad", message: res.message, title });
}

const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => listeners.delete(l);
};

export function Toaster() {
  const list = useSyncExternalStore(subscribe, () => toasts, () => toasts);
  return (
    <section className="toaster" aria-live="polite" aria-label="Notifications">
      {list.map((t) => (t.job ? <JobToast key={t.id} t={t} /> : <PlainToast key={t.id} t={t} />))}
    </section>
  );
}

function PlainToast({ t }: { t: Toast }) {
  useEffect(() => {
    const ms = t.tone === "bad" ? 9000 : 5000;
    const h = setTimeout(() => dismissToast(t.id), ms);
    return () => clearTimeout(h);
  }, [t.id, t.tone]);
  return (
    <div className={`toast tone-${t.tone}`} role={t.tone === "bad" ? "alert" : "status"}>
      <span className="toast__dot" aria-hidden="true" />
      <div className="toast__body">
        {t.title && <p className="toast__title">{t.title}</p>}
        <p>{t.message}</p>
      </div>
      <button type="button" className="toast__x" aria-label="Dismiss" onClick={() => dismissToast(t.id)}>
        <Icon name="close" size={14} />
      </button>
    </div>
  );
}

// Polls the job every two seconds until it settles, then refreshes the screen
// so the numbers it changed show up without a manual reload.
function JobToast({ t }: { t: Toast }) {
  const router = useRouter();
  const [snap, setSnap] = useState<JobSnapshot | null>(null);
  const [lost, setLost] = useState(0);
  useEffect(() => {
    if (!t.job) return;
    let stop = false;
    let h: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const s = await getJob(t.job!).catch(() => null);
      if (stop) return;
      if (!s) setLost((n) => n + 1);
      else setSnap(s);
      if (s?.done) {
        updateToast(t.id, { tone: s.ok ? "ok" : "bad" });
        router.refresh();
        h = setTimeout(() => dismissToast(t.id), s.ok ? 6000 : 15000);
        return;
      }
      h = setTimeout(tick, 2000);
    };
    tick();
    return () => {
      stop = true;
      clearTimeout(h);
    };
  }, [t.job, t.id, router]);

  const done = snap?.done;
  const tone: Tone = done ? (snap.ok ? "ok" : "bad") : "info";
  const pct = snap?.progress;
  return (
    <div className={`toast tone-${tone}`} role="status">
      <span className={`toast__dot${done ? "" : " toast__dot--live"}`} aria-hidden="true" />
      <div className="toast__body">
        <p className="toast__title">
          {t.title ?? "Background job"} · {done ? (snap.ok ? "finished" : snap.status) : snap?.status ?? "queued"}
        </p>
        <p>{done ? (snap.ok ? "Done — the screen is updated." : snap.detail ?? "It didn’t finish cleanly.") : snap?.detail ?? t.message}</p>
        {!done && (
          <div className="progress" aria-label="Progress">
            <span className={pct === undefined ? "progress__bar progress__bar--busy" : "progress__bar"} style={pct === undefined ? undefined : { width: `${pct}%` }} />
          </div>
        )}
        {lost > 3 && !snap && <p className="toast__meta">Can’t read this job’s status — it keeps running on the server.</p>}
        <p className="toast__meta num">job {t.job}</p>
      </div>
      <button type="button" className="toast__x" aria-label="Hide (the job keeps running)" onClick={() => dismissToast(t.id)}>
        <Icon name="close" size={14} />
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Running actions

/** Run a server action: pending state, toast, refresh on success. */
export function useAction<A extends unknown[], T>(
  action: (...args: A) => Promise<ActionResult<T>>,
  opts: { title?: string; quiet?: boolean; refresh?: boolean } = {},
) {
  const router = useRouter();
  const [pending, start] = useTransition();
  const [last, setLast] = useState<ActionResult<T> | null>(null);
  const mode = useActionsMode();
  const run = useCallback(
    (...args: A) =>
      new Promise<ActionResult<T>>((resolve) => {
        if (mode !== "on") {
          const res = { ok: false, message: LOCK_REASON[mode] } as ActionResult<T>;
          report(res, opts.title);
          setLast(res);
          return resolve(res);
        }
        start(async () => {
          let res: ActionResult<T>;
          try {
            res = await action(...args);
          } catch {
            res = { ok: false, message: "The dashboard couldn’t reach its server. Reload and try again." };
          }
          setLast(res);
          if (!opts.quiet || !res.ok) report(res, opts.title);
          if (res.ok && opts.refresh !== false) router.refresh();
          resolve(res);
        });
      }),
    [action, mode, opts.quiet, opts.refresh, opts.title, router, start],
  );
  return { run, pending, last, mode };
}

export interface Confirm {
  title: string;
  body: string;
  yes: string;
}

export function RunButton({
  action,
  label,
  icon,
  variant = "neutral",
  confirm,
  title,
  small,
  onDone,
}: {
  action: () => Promise<ActionResult<unknown>>;
  label: string;
  icon?: IconName;
  variant?: "primary" | "neutral" | "danger";
  confirm?: Confirm;
  /** Toast heading, e.g. "Re-index". */
  title?: string;
  small?: boolean;
  onDone?: (res: ActionResult<unknown>) => void;
}) {
  const { run, pending, mode } = useAction(action, { title: title ?? label });
  const [asking, setAsking] = useState(false);
  const go = async () => {
    setAsking(false);
    const res = await run();
    onDone?.(res);
  };
  const locked = mode !== "on";
  return (
    <>
      <button
        type="button"
        className={`btn btn--${variant}${small ? " btn--sm" : ""}`}
        disabled={pending || locked}
        aria-busy={pending}
        title={locked ? LOCK_REASON[mode] : undefined}
        onClick={() => (confirm ? setAsking(true) : go())}
      >
        {pending ? <span className="spin" aria-hidden="true" /> : icon ? <Icon name={icon} size={15} /> : null}
        {label}
      </button>
      {confirm && (
        <Dialog open={asking} onClose={() => setAsking(false)} title={confirm.title}>
          <p className="dialog__text">{confirm.body}</p>
          <div className="dialog__actions">
            <button type="button" className="btn btn--neutral" onClick={() => setAsking(false)}>
              Cancel
            </button>
            <button type="button" className={`btn btn--${variant === "danger" ? "danger" : "primary"}`} onClick={go} autoFocus>
              {confirm.yes}
            </button>
          </div>
        </Dialog>
      )}
    </>
  );
}

/** A visible reason under a group of controls when actions can't run. */
export function LockNote() {
  const mode = useActionsMode();
  if (mode === "on") return null;
  return <p className="lock-note">{LOCK_REASON[mode]}</p>;
}

// ---------------------------------------------------------------------------
// Dialog (native <dialog>: focus trap, Esc and backdrop for free)

export function Dialog({ open, onClose, title, children, wide }: { open: boolean; onClose: () => void; title: string; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      className={`dialog${wide ? " dialog--wide" : ""}`}
      onClose={onClose}
      onClick={(e) => e.target === ref.current && onClose()}
      aria-label={title}
    >
      <div className="dialog__box">
        <header className="dialog__head">
          <h2 className="dialog__title">{title}</h2>
          <button type="button" className="toast__x" aria-label="Close" onClick={onClose}>
            <Icon name="close" size={16} />
          </button>
        </header>
        {open && children}
      </div>
    </dialog>
  );
}

// ---------------------------------------------------------------------------
// Copyable multi-line block (configs, keys)

export function CodeBlock({ code, label, secret }: { code: string; label: string; secret?: boolean }) {
  const [done, setDone] = useState(false);
  return (
    <figure className={`codeblock${secret ? " codeblock--secret" : ""}`}>
      <figcaption className="codeblock__head">
        <span>{label}</span>
        <button
          type="button"
          className={`copy${done ? " is-done" : ""}`}
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(code);
              setDone(true);
              setTimeout(() => setDone(false), 1600);
            } catch {
              toast({ tone: "warn", message: "The browser blocked the clipboard — select the text and copy it by hand." });
            }
          }}
        >
          <Icon name={done ? "check" : "copy"} size={14} />
          {done ? "Copied" : "Copy"}
        </button>
      </figcaption>
      <pre>
        <code>{code}</code>
      </pre>
    </figure>
  );
}
