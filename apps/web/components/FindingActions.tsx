"use client";
import Link from "next/link";
import { useState } from "react";
import { Dialog, LockNote, useAction } from "./act";
import { useCapability } from "./Capabilities";
import { dismissFindings } from "@/lib/actions/findings";

export function FindingActions({ id, targets, codeAvailable }: { id: string; targets: { id: string; lastSeenAt: string }[]; codeAvailable: boolean }) {
  const supported = useCapability("finding_actions");
  const [open, setOpen] = useState(false), [reason, setReason] = useState("");
  const { run, pending, mode } = useAction(dismissFindings, { title: "Dismiss findings" });
  const [error, setError] = useState("");
  return <><div className="btn-row">
    {supported && codeAvailable ? <Link className="btn btn--neutral btn--sm" href={`/insights/${encodeURIComponent(id)}/code`}>Open code</Link> : <button type="button" className="btn btn--neutral btn--sm" disabled title={supported ? "This finding has no indexed file evidence with an explicit repository identity" : "Update the API to support finding actions"}>Open code</button>}
    <button type="button" className="btn btn--neutral btn--sm" onClick={() => setOpen(true)} disabled={!supported || !targets.length || mode !== "on"}>Dismiss{targets.length > 1 ? ` ${targets.length} duplicates` : ""}</button>
  </div><Dialog open={open} onClose={() => setOpen(false)} title="Dismiss finding"><form className="form" onSubmit={async e => { e.preventDefault(); const result = await run(targets, reason); if (result.ok) setOpen(false); else setError(result.message); }}>
    <p>This closes the selected finding versions. The evidence remains in the database, and the reason and verified caller are recorded.</p>
    <label className="field">Reason<textarea className="input" required maxLength={1000} value={reason} onChange={e => setReason(e.target.value)} /></label>
    {error && <p role="alert">{error}</p>}<LockNote /><button type="submit" className="btn btn--primary" disabled={pending || !reason.trim() || mode !== "on"}>Dismiss with reason</button>
  </form></Dialog></>;
}
