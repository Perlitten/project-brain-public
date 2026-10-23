"use client";

// Try context assembly for a task from /packs, without the detour to Setup:
// the same action as Setup's "Brief an agent on a real task" step.
import Link from "next/link";
import { useState } from "react";
import { Dialog, LockNote, useAction } from "./act";
import { Icon } from "./Icon";
import { buildFirstPack } from "@/lib/actions/setup";
import "./memory-ux.css";

export function PackTester({ repoPath, label = "Test a context pack" }: { repoPath?: string; label?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className="btn btn--neutral" onClick={() => setOpen(true)}>
        <Icon name="layers" size={15} />
        {label}
      </button>
      <Dialog open={open} onClose={() => setOpen(false)} title="Test a context pack" wide>
        <PackForm repoPath={repoPath} />
      </Dialog>
    </>
  );
}

function PackForm({ repoPath }: { repoPath?: string }) {
  const [task, setTask] = useState("");
  const [built, setBuilt] = useState<{ files?: string[]; id?: number } | null>(null);
  const { run, pending, mode, last } = useAction(buildFirstPack, { title: "Context pack" });
  const length = task.trim().length;
  return (
    <form
      className="form form--wide"
      onSubmit={async (e) => {
        e.preventDefault();
        const res = await run(task, repoPath);
        if (res.ok) setBuilt(res.data ?? {});
      }}
    >
      <p className="dialog__text">
        See which files, decisions and rules an agent would get for a task.{" "}
        {repoPath ? <>Built for <span className="mono">{repoPath}</span>.</> : "Built for Brain’s default repository."} The pack is
        saved to this list; nothing in your code changes.
      </p>
      <label className="field" htmlFor="pack-test-task">
        <span className="field__label">Task</span>
        <textarea
          id="pack-test-task"
          className="textarea"
          rows={3}
          value={task}
          onChange={(e) => setTask(e.target.value)}
          placeholder="e.g. Add rate limiting to the login endpoint"
          maxLength={2000}
          data-autofocus
        />
        <span className="field__hint">
          {length > 0 && length < 8
            ? `${8 - length} more character${8 - length === 1 ? "" : "s"} to go.`
            : "Write it like a ticket: what to change and where. Brain only reads your code."}
        </span>
      </label>
      {last && !last.ok && <p role="alert">{last.message}</p>}
      {built && (
        <div className="callout tone-ok">
          <b>Context pack ready{built.files?.length ? ` — ${built.files.length} files picked` : ""}.</b>
          {built.files && built.files.length > 0 && (
            <ul className="filelist pack-test__files">
              {built.files.map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ul>
          )}
          {built.id !== undefined && (
            <Link className="inline-link" href={`/packs/${encodeURIComponent(String(built.id))}`}>
              Open the full pack
            </Link>
          )}
        </div>
      )}
      <LockNote />
      <div className="dialog__actions">
        <button type="submit" className="btn btn--primary" disabled={pending || mode !== "on" || length < 8}>
          {pending ? <span className="spin" aria-hidden="true" /> : <Icon name="layers" size={15} />}
          {pending ? "Building…" : built ? "Build again" : "Build context pack"}
        </button>
      </div>
    </form>
  );
}
