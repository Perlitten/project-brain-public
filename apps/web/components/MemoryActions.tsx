"use client";
// Edit, accept and retire controls for one decision or rule on the Memory page.
// Each write sends the version the page showed (lib/actions/memory.ts).
import { useState } from "react";
import { Dialog, LOCK_REASON, LockNote, RunButton, useAction, useActionsMode } from "./act";
import { CopyCommand } from "./CopyCommand";
import "./memory-ux.css";
import { acceptDecision, retireDecision, retireRule, saveDecision, saveRule } from "@/lib/actions/memory";
import type { Decision, Rule } from "@/lib/types";

/** Opens the edit dialog; locked like every other action outside "on" mode. */
function EditButton({ name, onOpen }: { name: string; onOpen: () => void }) {
  const mode = useActionsMode();
  const locked = mode !== "on";
  return <button type="button" className="btn btn--neutral btn--sm" disabled={locked} title={locked ? LOCK_REASON[mode] : `Edit ${name}`} onClick={onOpen}>Edit</button>;
}

export function DecisionActions({ decision }: { decision: Decision }) {
  const [editing, setEditing] = useState(false);
  const write = decision.write;
  if (!write || decision.status === "superseded") return null;
  return (
    <div className="card__actions">
      {decision.status === "proposed" && <RunButton small variant="primary" action={() => acceptDecision(write)} label="Accept" title={`Accept “${decision.title}”`} />}
      <EditButton name={decision.title} onOpen={() => setEditing(true)} />
      <RunButton small variant="danger" action={() => retireDecision(write)} label="Retire" title={`Retire “${decision.title}”`}
        confirm={{ title: "Retire this decision?", body: "Agents stop receiving it. The record stays in Brain’s history.", yes: "Retire decision" }} />
      <Dialog open={editing} onClose={() => setEditing(false)} title="Edit decision" wide>
        <DecisionForm decision={decision} onDone={() => setEditing(false)} />
      </Dialog>
    </div>
  );
}

function DecisionForm({ decision, onDone }: { decision: Decision; onDone: () => void }) {
  const write = decision.write!;
  const [description, setDescription] = useState(write.description);
  const [reason, setReason] = useState(write.reason);
  const { run, pending, mode, last } = useAction(saveDecision, { title: "Edit decision" });
  return (
    <form className="form form--wide" onSubmit={async (e) => { e.preventDefault(); if ((await run(write, { description, reason })).ok) onDone(); }}>
      <p className="dialog__text"><b>{decision.title}</b> · <span className="mono">{decision.scope}</span></p>
      <label className="field">What was decided<textarea className="textarea" required rows={6} maxLength={10000} value={description} onChange={(e) => setDescription(e.target.value)} data-autofocus /></label>
      <label className="field">Why<textarea className="textarea" rows={3} maxLength={5000} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
      {last && !last.ok && <p role="alert">{last.message}</p>}
      <LockNote />
      <div className="dialog__actions">
        <button type="button" className="btn btn--neutral" onClick={onDone}>Cancel</button>
        <button type="submit" className="btn btn--primary" disabled={pending || mode !== "on" || !description.trim()}>{pending ? "Saving…" : "Save decision"}</button>
      </div>
    </form>
  );
}

export function RuleActions({ rule }: { rule: Rule }) {
  const [editing, setEditing] = useState(false);
  const write = rule.write;
  if (!write) return null;
  return (
    <div className="row-actions row-actions--inline">
      <EditButton name={rule.id} onOpen={() => setEditing(true)} />
      <RunButton small variant="danger" action={() => retireRule(write)} label="Retire" title={`Retire ${rule.id}`}
        confirm={{ title: "Retire this rule?", body: "Agents stop receiving it and reviews stop checking it. The record stays in Brain’s history.", yes: "Retire rule" }} />
      <Dialog open={editing} onClose={() => setEditing(false)} title="Edit rule" wide>
        <RuleForm rule={rule} onDone={() => setEditing(false)} />
      </Dialog>
    </div>
  );
}

// The page groups stored severities into three; picking a group stores its plain value.
const GROUPS: { value: Rule["severity"]; stored: string; label: string }[] = [
  { value: "block", stored: "high", label: "Blocks the change" },
  { value: "warn", stored: "medium", label: "Warns" },
  { value: "advise", stored: "low", label: "Suggests" },
];

function RuleForm({ rule, onDone }: { rule: Rule; onDone: () => void }) {
  const write = rule.write!;
  const [description, setDescription] = useState(write.description || rule.rule);
  const [group, setGroup] = useState<Rule["severity"]>(rule.severity);
  const { run, pending, mode, last } = useAction(saveRule, { title: "Edit rule" });
  // An untouched choice keeps the stored value (e.g. "critical" stays critical).
  const severity = group === rule.severity ? write.severity : GROUPS.find((g) => g.value === group)!.stored;
  return (
    <form className="form form--wide" onSubmit={async (e) => { e.preventDefault(); if ((await run(write, { description, severity })).ok) onDone(); }}>
      <p className="dialog__text"><span className="mono">{rule.id}</span> · {rule.scope}</p>
      <label className="field">Rule<textarea className="textarea" required rows={5} maxLength={10000} value={description} onChange={(e) => setDescription(e.target.value)} data-autofocus /></label>
      <label className="field">If broken
        <select className="select" value={group} onChange={(e) => setGroup(e.target.value as Rule["severity"])}>
          {GROUPS.map((g) => <option key={g.value} value={g.value}>{g.label}</option>)}
        </select>
      </label>
      {last && !last.ok && <p role="alert">{last.message}</p>}
      <LockNote />
      <div className="dialog__actions">
        <button type="button" className="btn btn--neutral" onClick={onDone}>Cancel</button>
        <button type="submit" className="btn btn--primary" disabled={pending || mode !== "on" || !description.trim()}>{pending ? "Saving…" : "Save rule"}</button>
      </div>
    </form>
  );
}

// New decisions and rules come from agents and the CLI; this page edits what
// they recorded. The hint names the exact tool and command for each.
export function RecordHint({ kind, repoPath }: { kind: "decision" | "rule"; repoPath?: string }) {
  const scopedDecision = kind === "decision" && Boolean(repoPath);
  const command =
    kind === "decision"
      ? `brain decision add "<title>" --desc "<what was decided>" --reason "<why>"`
      : `brain rules add "<name>"${repoPath ? ` --repo ${repoPath.includes(" ") ? `"${repoPath}"` : repoPath}` : ""} --desc "<the rule>" --severity high`;
  return (
    <div className="record-hint">
      <p className="record-hint__text">
        Agents call the <code className="mono">{kind === "decision" ? "record_decision" : "record_rule"}</code> tool.{scopedDecision ? " Use it for this project." : " From a terminal:"}
      </p>
      {scopedDecision
        ? <p className="field__hint">Use the agent tool on this project; the CLI does not currently accept a repository scope for decisions.</p>
        : <CopyCommand command={command} />}
    </div>
  );
}
