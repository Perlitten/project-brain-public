"use client";
// Record a decision or a rule from the dashboard. One form, two kinds; a
// finding can open it pre-filled (/memory?new=rule&title=…&description=…).
import { useState } from "react";
import { LockNote, useAction } from "./act";
import { createDecision, createRule } from "@/lib/actions/memory";

export type MemoryKind = "decision" | "rule";

export interface MemoryPrefill {
  kind: MemoryKind;
  title?: string;
  description?: string;
}

interface Project {
  slug: string;
  name: string;
  path: string;
}

// The page groups stored severities into three; picking a group stores its plain value.
const SEVERITY = [
  { stored: "high", label: "Blocks the change" },
  { stored: "medium", label: "Warns" },
  { stored: "low", label: "Suggests" },
];

export function NewMemoryForm({ projects, repoPath, prefill, onDone }: { projects: Project[]; repoPath?: string; prefill?: MemoryPrefill; onDone?: () => void }) {
  const [kind, setKind] = useState<MemoryKind>(prefill?.kind ?? "decision");
  const [title, setTitle] = useState(prefill?.title ?? "");
  const [description, setDescription] = useState(prefill?.description ?? "");
  const [reason, setReason] = useState("");
  const [severity, setSeverity] = useState("medium");
  const [scope, setScope] = useState(repoPath ?? (projects.length === 1 ? projects[0].path : ""));
  const decision = useAction(createDecision, { title: "Record decision" });
  const rule = useAction(createRule, { title: "Record rule" });
  const pending = decision.pending || rule.pending;
  const mode = decision.mode;
  const last = kind === "decision" ? decision.last : rule.last;
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const res = kind === "decision"
      ? await decision.run({ title, description, reason, repoPath: scope || null })
      : await rule.run({ name: title, description, severity, repoPath: scope || null });
    if (res.ok) {
      setTitle("");
      setDescription("");
      setReason("");
      onDone?.();
    }
  };
  return (
    <form className="form form--wide new-memory" onSubmit={submit} autoComplete="off">
      <div className="form__row">
        <div className="field">
          <span className="field__label">What to record</span>
          <div className="seg seg--fill" role="group" aria-label="Kind">
            <button type="button" aria-pressed={kind === "decision"} onClick={() => setKind("decision")}>Decision</button>
            <button type="button" aria-pressed={kind === "rule"} onClick={() => setKind("rule")}>Rule</button>
          </div>
          <span className="field__hint">{kind === "decision" ? "Something the team settled: agents read it before they start." : "Something a change must respect: reviews check it, agents read it."}</span>
        </div>
        <label className="field">
          <span className="field__label">Applies to</span>
          <select className="select" value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="">Every project</option>
            {projects.map((p) => (
              <option key={p.slug} value={p.path}>{p.name}</option>
            ))}
          </select>
        </label>
        {kind === "rule" && (
          <label className="field">
            <span className="field__label">If broken</span>
            <select className="select" value={severity} onChange={(e) => setSeverity(e.target.value)}>
              {SEVERITY.map((s) => (
                <option key={s.stored} value={s.stored}>{s.label}</option>
              ))}
            </select>
          </label>
        )}
      </div>
      <label className="field">
        <span className="field__label">{kind === "decision" ? "Title" : "Name"}</span>
        <input className="input" required maxLength={500} value={title} onChange={(e) => setTitle(e.target.value)} placeholder={kind === "decision" ? "Retries live in one module" : "No direct DB access from handlers"} data-autofocus />
      </label>
      <label className="field">
        <span className="field__label">{kind === "decision" ? "What was decided" : "The rule"}</span>
        <textarea className="textarea" required rows={4} maxLength={10000} value={description} onChange={(e) => setDescription(e.target.value)} placeholder={kind === "decision" ? "What agents should do from now on, in one or two sentences." : "What a change must or must not do, in words a reviewer can check."} />
      </label>
      {kind === "decision" && (
        <label className="field">
          <span className="field__label">Why <em>optional</em></span>
          <textarea className="textarea" rows={2} maxLength={5000} value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>
      )}
      {last && !last.ok && <p role="alert">{last.message}</p>}
      <LockNote />
      <div className="btn-row">
        <button type="submit" className="btn btn--primary" disabled={pending || mode === "locked" || !title.trim() || !description.trim()}>
          {pending ? "Recording…" : kind === "decision" ? "Record decision" : "Record rule"}
        </button>
        {onDone && <button type="button" className="btn btn--neutral" onClick={onDone}>Cancel</button>}
      </div>
    </form>
  );
}
