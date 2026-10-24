"use client";
import Link from "next/link";
import { useState } from "react";
import { Dialog, LockNote, useAction } from "./act";
import { useCapability } from "./Capabilities";
import { dismissFindings } from "@/lib/actions/findings";

export function FindingActions({ id, targets, codeAvailable, title, evidence, module }: { id: string; targets: { id: string; lastSeenAt: string }[]; codeAvailable: boolean; title: string; evidence: string; module?: string }) {
  const supported = useCapability("finding_actions");
  const [open, setOpen] = useState(false), [reason, setReason] = useState("");
  const { run, pending, mode } = useAction(dismissFindings, { title: "Dismiss findings" });
  const [error, setError] = useState("");
  // A finding is only useful once it becomes a rule (so it stops recurring) or a task (so someone fixes it).
  const where = module ? ` (${module})` : "";
  const ruleHref = `/memory?new=rule&title=${encodeURIComponent(title.slice(0, 200))}&description=${encodeURIComponent(evidence.slice(0, 2000))}#record`;
  const taskHref = `/runs?new=task&title=${encodeURIComponent(`Fix: ${title}`.slice(0, 300))}&goal=${encodeURIComponent(`${evidence}${where}`.slice(0, 4000))}`;
  return <><div className="btn-row">
    <Link className="btn btn--primary btn--sm" href={taskHref} title="Record an agent task for this finding">Record task</Link>
    <Link className="btn btn--neutral btn--sm" href={ruleHref} title="Record a rule so reviews catch this from now on">Make a rule</Link>
    {supported && codeAvailable ? <Link className="btn btn--neutral btn--sm" href={`/insights/${encodeURIComponent(id)}/code`}>Open code</Link> : <button type="button" className="btn btn--neutral btn--sm" disabled title={supported ? "This finding has no indexed file evidence with an explicit repository identity" : "Update the API to support finding actions"}>Open code</button>}
    <button type="button" className="btn btn--neutral btn--sm" onClick={() => setOpen(true)} disabled={!supported || !targets.length || mode === "locked"}>Dismiss{targets.length > 1 ? ` ${targets.length} duplicates` : ""}</button>
  </div><Dialog open={open} onClose={() => setOpen(false)} title="Dismiss finding"><form className="form" onSubmit={async e => { e.preventDefault(); const result = await run(targets, reason); if (result.ok) setOpen(false); else setError(result.message); }}>
    <p>This closes the selected finding versions. The evidence remains in the database, and the reason and verified caller are recorded.</p>
    <label className="field">Reason<textarea className="input" required maxLength={1000} value={reason} onChange={e => setReason(e.target.value)} /></label>
    {error && <p role="alert">{error}</p>}<LockNote /><button type="submit" className="btn btn--primary" disabled={pending || !reason.trim() || mode === "locked"}>Dismiss with reason</button>
  </form></Dialog></>;
}
