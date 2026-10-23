"use client";
// Team page controls: add a member (their dashboard key is shown once), set a
// member's role per project, revoke a member. Rendered for team admins only;
// Brain checks every call with the admin's own key anyway.

import { useState, type CSSProperties } from "react";
import { CodeBlock, Dialog, LockNote, RunButton, useAction } from "@/components/act";
import { Icon } from "@/components/Icon";
import { Chip } from "@/components/ui";
import { addMember, removeFromProject, revokeMember, setProjectRole } from "@/lib/actions/team";
import type { NewKey } from "@/lib/actions/admin";
import { EXPIRY_CHOICES } from "@/lib/scopes";
import { PROJECT_ROLES, ROLE_WORD, type ProjectRole } from "@/lib/team-scopes";
import type { TeamMemberRow, TeamProjectRow } from "@/lib/team-view";
import "./team-ux.css";

export function AddMemberForm() {
  const [name, setName] = useState("");
  const [admin, setAdmin] = useState(false);
  const [existing, setExisting] = useState(false);
  const [ttl, setTtl] = useState(0);
  const [key, setKey] = useState<NewKey | null>(null);
  const { run, pending, mode } = useAction(addMember, { title: "Add member" });
  return (
    <>
      <form
        className="form form--wide"
        autoComplete="off"
        onSubmit={async (e) => {
          e.preventDefault();
          const res = await run({ name, admin, mintKey: !existing, ttlDays: ttl || undefined });
          if (res.ok) {
            setName("");
            setAdmin(false);
            if (res.data) setKey(res.data);
          }
        }}
      >
        <div className="form__row">
          <label className="field">
            <span className="field__label">Name</span>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="alice" maxLength={120} spellCheck={false} />
            <span className="field__hint">{existing ? "The name of a person Brain already knows." : "How the team will recognise them."}</span>
          </label>
          <div className="field">
            <span className="field__label">Key</span>
            <div className="seg seg--fill" role="group" aria-label="Key">
              <button type="button" aria-pressed={!existing} onClick={() => setExisting(false)}>
                Issue a new key
              </button>
              <button type="button" aria-pressed={existing} onClick={() => setExisting(true)}>
                Use an existing person
              </button>
            </div>
            <span className="field__hint">{existing ? "They keep the keys they have." : "You’ll see the key once, right after."}</span>
          </div>
          {!existing && (
            <label className="field">
              <span className="field__label">Expires</span>
              <select className="select" value={ttl} onChange={(e) => setTtl(Number(e.target.value))}>
                {EXPIRY_CHOICES.map((c) => (
                  <option key={c.days} value={c.days}>
                    {c.label}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
        <label className="check">
          <input type="checkbox" checked={admin} onChange={(e) => setAdmin(e.target.checked)} />
          Team admin — can add and revoke members and change project roles
        </label>
        <div className="btn-row">
          <button type="submit" className="btn btn--primary" disabled={pending || mode !== "on" || !name.trim()}>
            {pending ? <span className="spin" aria-hidden="true" /> : <Icon name="plus" size={15} />}
            {existing ? "Add to the team" : "Add member and issue key"}
          </button>
        </div>
        <LockNote />
      </form>
      <MemberKeyDialog k={key} onClose={() => setKey(null)} />
    </>
  );
}

function MemberKeyDialog({ k, onClose }: { k: NewKey | null; onClose: () => void }) {
  return (
    <Dialog open={Boolean(k)} onClose={onClose} title={`Dashboard key for “${k?.name ?? "member"}”`}>
      {k && (
        <div className="form">
          <div className="callout tone-warn">
            <b>Copy it now — Brain shows this key only once.</b>
            <span>Lost it? Revoke the member and add them again with a new key.</span>
          </div>
          <CodeBlock label="Member key" code={k.apiKey} secret />
          <p className="field__hint">
            Send it to them privately. They open this dashboard and sign in with it as their member key; the same key works for their agents and the
            CLI. Give them a role in a project below so they can see its repositories.
          </p>
          <div className="dialog__actions">
            <button type="button" className="btn btn--primary" onClick={onClose}>
              I’ve copied it
            </button>
          </div>
        </div>
      )}
    </Dialog>
  );
}

export function MemberTable({ members, projects, selfId }: { members: TeamMemberRow[]; projects: TeamProjectRow[]; selfId: number | null }) {
  const live = projects.filter((p) => !p.disabled);
  return (
    <div className="table-wrap team-members-wrap">
      <table className="table team-members">
        <caption className="sr-only">Team members</caption>
        <thead>
          <tr>
            <th scope="col">Member</th>
            <th scope="col">Status</th>
            {live.map((p) => (
              <th scope="col" key={p.id} className="team-members__project" title={p.name}>
                {p.name}
              </th>
            ))}
            <th scope="col" className="r">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {members.map((m, n) => (
            <tr key={m.id} style={{ "--i": Math.min(n, 12) } as CSSProperties}>
              <td>
                <span className="mono">{m.name}</span>
                <span className="sub">{m.verifiedAt ? `member since ${m.verifiedAt}` : "member"}</span>
              </td>
              <td>
                <span className="chips">
                  {m.revoked ? <Chip tone="idle">revoked</Chip> : <Chip tone="ok">active</Chip>}
                  {m.admin && <Chip tone="info">admin</Chip>}
                  {m.id === selfId && (
                    <Chip tone="idle" dot={false}>
                      you
                    </Chip>
                  )}
                </span>
              </td>
              {live.map((p) => (
                <td key={p.id}>{m.revoked ? <span className="sub">—</span> : p.unread ? <span className="sub" title="Brain didn’t return this project’s members just now. Reload in a moment.">unavailable</span> : <RoleSelect project={p} member={m} />}</td>
              ))}
              <td className="r">
                {!m.revoked && m.id !== selfId && (
                  <RunButton
                    small
                    variant="danger"
                    action={() => revokeMember(m.id)}
                    label="Revoke"
                    title={`Revoke “${m.name}”`}
                    confirm={{
                      title: `Revoke “${m.name}”?`,
                      body: "They lose the team at once: their next dashboard request signs them out, and their keys no longer reach team projects. Adding them again restores access.",
                      yes: "Revoke",
                    }}
                  />
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RoleSelect({ project, member }: { project: TeamProjectRow; member: TeamMemberRow }) {
  const current = member.roles[project.id] ?? "";
  const set = useAction(setProjectRole, { title: `Role in ${project.name}` });
  const drop = useAction(removeFromProject, { title: `Role in ${project.name}` });
  const pending = set.pending || drop.pending;
  return (
    <select
      className="select"
      aria-label={`${member.name}’s role in ${project.name}`}
      value={current}
      disabled={pending || set.mode !== "on"}
      title={current ? ROLE_WORD[current as ProjectRole] : "Not in this project"}
      onChange={(e) => {
        const next = e.target.value;
        if (next === current) return;
        if (next === "") void drop.run(project.id, member.id);
        else void set.run(project.id, member.id, next as ProjectRole);
      }}
    >
      <option value="">No access</option>
      {PROJECT_ROLES.map((r) => (
        <option key={r} value={r}>
          {r[0].toUpperCase() + r.slice(1)}
        </option>
      ))}
    </select>
  );
}
