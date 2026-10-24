"use client";
// Record an agent task from the dashboard. The harness ledger stores it; an
// external dispatcher still has to launch it. A finding can open the dialog
// pre-filled (/runs?new=task&title=…&goal=…).
import { useState } from "react";
import { Dialog, LockNote, useAction } from "./act";
import { createTask } from "@/lib/actions/tasks";

export interface TaskPrefill {
  title?: string;
  goal?: string;
}

interface Project {
  slug: string;
  name: string;
  path: string;
}

const PRIORITIES = [
  { value: "P0", label: "P0 — drop everything" },
  { value: "P1", label: "P1 — next" },
  { value: "P2", label: "P2 — normal" },
  { value: "P3", label: "P3 — when idle" },
];

export function NewTaskButton({ projects, prefill, small }: { projects: Project[]; prefill?: TaskPrefill; small?: boolean }) {
  const [open, setOpen] = useState(Boolean(prefill));
  const [title, setTitle] = useState(prefill?.title ?? "");
  const [goal, setGoal] = useState(prefill?.goal ?? "");
  const [repoPath, setRepoPath] = useState(projects.length === 1 ? projects[0].path : "");
  const [agent, setAgent] = useState("");
  const [priority, setPriority] = useState("P2");
  const { run, pending, mode, last } = useAction(createTask, { title: "Record an agent task" });
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const res = await run({ title, goal, repoPath, targetAgent: agent || undefined, priority });
    if (res.ok) {
      setOpen(false);
      setTitle("");
      setGoal("");
      setAgent("");
    }
  };
  const locked = mode === "locked";
  return (
    <>
      <button type="button" className={`btn btn--primary${small ? " btn--sm" : ""}`} onClick={() => setOpen(true)} disabled={locked} title={locked ? "Actions are locked until the dashboard has a password." : undefined}>
        New task
      </button>
      <Dialog open={open} onClose={() => setOpen(false)} title="Record an agent task" wide>
        <form className="form form--wide" onSubmit={submit} autoComplete="off">
          <p className="dialog__text">
            Brain records it in the harness ledger. This does not start the work: launch the recorded task with your harness dispatcher.
          </p>
          <label className="field">
            <span className="field__label">Title</span>
            <input className="input" required maxLength={300} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Move retry logic into one module" data-autofocus />
          </label>
          <label className="field">
            <span className="field__label">What done looks like</span>
            <textarea className="textarea" required rows={5} maxLength={10000} value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="The agent reads this. Say what to change, where, and how to tell it worked." />
          </label>
          <div className="form__row">
            <label className="field">
              <span className="field__label">Project</span>
              {projects.length > 0 ? (
                <select className="select" required value={repoPath} onChange={(e) => setRepoPath(e.target.value)}>
                  <option value="" disabled>Pick a project</option>
                  {projects.map((p) => (
                    <option key={p.slug} value={p.path}>{p.name}</option>
                  ))}
                </select>
              ) : (
                <input className="input input--mono" required value={repoPath} onChange={(e) => setRepoPath(e.target.value)} placeholder="/path/to/repository" />
              )}
            </label>
            <label className="field">
              <span className="field__label">Agent <em>optional</em></span>
              <input className="input input--mono" maxLength={100} value={agent} onChange={(e) => setAgent(e.target.value)} placeholder="any agent" />
              <span className="field__hint">Leave empty and whichever agent asks first takes it.</span>
            </label>
            <label className="field">
              <span className="field__label">Priority</span>
              <select className="select" value={priority} onChange={(e) => setPriority(e.target.value)}>
                {PRIORITIES.map((p) => (
                  <option key={p.value} value={p.value}>{p.label}</option>
                ))}
              </select>
            </label>
          </div>
          {last && !last.ok && <p role="alert">{last.message}</p>}
          <LockNote />
          <div className="dialog__actions">
            <button type="button" className="btn btn--neutral" onClick={() => setOpen(false)}>Cancel</button>
            <button type="submit" className="btn btn--primary" disabled={pending || locked || !title.trim() || !goal.trim() || !repoPath}>
              {pending ? "Recording…" : "Record task"}
            </button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
