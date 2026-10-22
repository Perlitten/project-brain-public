"use client";
// Team page: invite someone (they pick up their own key on /invite), see every
// invitation's state, revoke one. Rendered for team admins only; Brain checks
// every call with the admin's own key anyway.

import { useState, type CSSProperties } from "react";
import { utcTime } from "@/lib/time";
import { CodeBlock, Dialog, LockNote, RunButton, useAction } from "@/components/act";
import { Icon } from "@/components/Icon";
import { Chip } from "@/components/ui";
import { createInvitation, revokeInvitation, type NewInvitation } from "@/lib/actions/team";
import { INVITE_EXPIRY_CHOICES, STATE_NOTE, STATE_TONE, inviteLink, revokesMember } from "@/lib/invitations";
import type { InvitationRow } from "@/lib/invitation-view";
import { PROJECT_ROLES, ROLE_WORD, type ProjectRole } from "@/lib/team-scopes";
import type { TeamProjectRow } from "@/lib/team-view";

const MAX_PROJECTS = 16;
const cap = (s: string) => s[0].toUpperCase() + s.slice(1);
/** Brain stores UTC; minutes are enough to tell invitations apart. */
const when = (iso: string | null) => iso ? utcTime(iso) : "—";

export function InviteForm({ projects, projectsAvailable = true }: { projects: TeamProjectRow[]; projectsAvailable?: boolean }) {
  const live = projects.filter((p) => !p.disabled);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [role, setRole] = useState<ProjectRole>("contributor");
  const [admin, setAdmin] = useState(false);
  const [hours, setHours] = useState(72);
  const [made, setMade] = useState<(NewInvitation & { link: string }) | null>(null);
  const { run, pending, mode } = useAction(createInvitation, { title: "Invite member" });
  const toggle = (id: string, on: boolean) => setPicked((now) => (on ? [...now, id] : now.filter((p) => p !== id)));
  return (
    <>
      <form
        className="form form--wide"
        autoComplete="off"
        onSubmit={async (e) => {
          e.preventDefault();
          const res = await run({ name, email, projects: picked, role, admin, expiresHours: hours });
          if (res.ok && res.data) {
            setMade({ ...res.data, link: inviteLink(window.location.origin, res.data.token) });
            setName("");
            setEmail("");
            setPicked([]);
            setAdmin(false);
          }
        }}
      >
        <div className="form__row">
          <label className="field">
            <span className="field__label">
              Name <em>optional</em>
            </span>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="alice" maxLength={64} spellCheck={false} disabled={!projectsAvailable} />
            <span className="field__hint">Their member name. Letters, digits and . _ @ + -, starting with a letter. Left empty, Brain picks one.</span>
          </label>
          <label className="field">
            <span className="field__label">
              Email <em>optional</em>
            </span>
            <input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="alice@example.com" maxLength={254} spellCheck={false} disabled={!projectsAvailable} />
            <span className="field__hint">A note for you: Brain doesn’t send mail, and whoever holds the link can accept it.</span>
          </label>
          <label className="field">
            <span className="field__label">Valid for</span>
            <select className="select" value={hours} onChange={(e) => setHours(Number(e.target.value))} disabled={!projectsAvailable}>
              {INVITE_EXPIRY_CHOICES.map((c) => (
                <option key={c.hours} value={c.hours}>
                  {c.label}
                </option>
              ))}
            </select>
            <span className="field__hint">Not accepted by then, the link stops working.</span>
          </label>
        </div>
        <div className="form__row">
          <div className="field">
            <span className="field__label">Projects</span>
            {!projectsAvailable ? (
              <span className="field__hint">Projects couldn’t be read right now, so invitation creation is paused. Reload before inviting someone.</span>
            ) : live.length === 0 ? (
              <span className="field__hint">No projects yet. They join the team with no project; give them a role once there is one.</span>
            ) : (
              <div className="btn-row" role="group" aria-label="Projects">
                {live.map((p) => (
                  <label className="check" key={p.id}>
                    <input
                      type="checkbox"
                      checked={picked.includes(p.id)}
                      disabled={!projectsAvailable || (!picked.includes(p.id) && picked.length >= MAX_PROJECTS)}
                      onChange={(e) => toggle(p.id, e.target.checked)}
                    />
                    {p.name}
                  </label>
                ))}
              </div>
            )}
          </div>
          <label className="field">
            <span className="field__label">Role in those projects</span>
            <select className="select" value={role} onChange={(e) => setRole(e.target.value as ProjectRole)} disabled={!projectsAvailable || picked.length === 0}>
              {PROJECT_ROLES.map((r) => (
                <option key={r} value={r}>
                  {cap(r)}
                </option>
              ))}
            </select>
            <span className="field__hint">{picked.length ? ROLE_WORD[role] : "Pick a project to give them a role in it."}</span>
          </label>
        </div>
        <label className="check">
          <input type="checkbox" checked={admin} onChange={(e) => setAdmin(e.target.checked)} disabled={!projectsAvailable} />
          Team admin — can add and revoke members and change project roles
        </label>
        <div className="btn-row">
          <button type="submit" className="btn btn--primary" disabled={!projectsAvailable || pending || mode !== "on"}>
            {pending ? <span className="spin" aria-hidden="true" /> : <Icon name="plus" size={15} />}
            Create invitation
          </button>
        </div>
        <LockNote />
      </form>
      <InvitationDialog made={made} onClose={() => setMade(null)} />
    </>
  );
}

function InvitationDialog({ made, onClose }: { made: (NewInvitation & { link: string }) | null; onClose: () => void }) {
  return (
    <Dialog open={Boolean(made)} onClose={onClose} title={`Invitation for “${made?.name ?? "member"}”`}>
      {made && (
        <div className="form">
          <div className="callout tone-warn">
            <b>Copy it now — Brain shows this link only once.</b>
            <span>Lost it? Revoke the invitation below and create a new one.</span>
          </div>
          <CodeBlock label="Invitation link" code={made.link} secret />
          <p className="field__hint">
            Send it to them privately{made.expiresAt ? `; it works until ${when(made.expiresAt)}, once` : ", it works once"}. Opening it on this dashboard
            makes them a member and signs them in with their own key. The token is in the part after <code>#</code>, which browsers don’t send to servers.
          </p>
          <CodeBlock label="Or the token alone, for the CLI (brain team invite accept)" code={made.token} secret />
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

export function InvitationTable({ invitations, projects, selfId }: { invitations: InvitationRow[]; projects: TeamProjectRow[]; selfId: number | null }) {
  const names = new Map(projects.map((p) => [p.id, p.name]));
  return (
    <div className="table-wrap">
      <table className="table team-invitations">
        <caption className="sr-only">Invitations</caption>
        <thead>
          <tr>
            <th scope="col">Invitee</th>
            <th scope="col">State</th>
            <th scope="col">Access</th>
            <th scope="col">Dates</th>
            <th scope="col" className="r">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {invitations.map((inv, n) => {
            const projectNames = inv.projectIds.map((id) => names.get(id) ?? "a removed project");
            const withMember = revokesMember(inv.state);
            return (
              <tr key={inv.id} style={{ "--i": Math.min(n, 12) } as CSSProperties}>
                <td>
                  <span className="mono">{inv.name}</span>
                  {inv.email && <span className="sub">{inv.email}</span>}
                </td>
                <td>
                  <span className="chips" title={STATE_NOTE[inv.state]}>
                    <Chip tone={STATE_TONE[inv.state]}>{inv.state}</Chip>
                    {inv.admin && <Chip tone="info">admin</Chip>}
                  </span>
                  <span className="sub">{STATE_NOTE[inv.state]}</span>
                </td>
                <td>
                  {projectNames.length ? (
                    <>
                      <span>{cap(inv.role)}</span>
                      <span className="sub">{projectNames.join(", ")}</span>
                    </>
                  ) : (
                    <span className="sub">No project</span>
                  )}
                </td>
                <td>
                  <span className="sub">created {when(inv.createdAt)}</span>
                  {inv.acceptedAt ? (
                    <span className="sub">accepted {when(inv.acceptedAt)}</span>
                  ) : inv.state === "invited" || inv.state === "expired" ? (
                    <span className="sub">{inv.state === "expired" ? "expired" : "expires"} {when(inv.expiresAt)}</span>
                  ) : null}
                  {inv.revokedAt && <span className="sub">revoked {when(inv.revokedAt)}</span>}
                </td>
                <td className="r">
                  {inv.state !== "revoked" && !(withMember && inv.memberId !== null && inv.memberId === selfId) && (
                    <RunButton
                      small
                      variant="danger"
                      action={() => revokeInvitation(inv.id, withMember)}
                      label={withMember ? "Revoke member" : "Revoke"}
                      title={`Revoke the invitation for “${inv.name}”`}
                      confirm={
                        withMember
                          ? {
                              title: `Revoke “${inv.name}” and their invitation?`,
                              body: "This invitation was accepted, so revoking it revokes the member it created too: their team membership, every project role and every key of theirs stop working at once.",
                              yes: "Revoke member",
                            }
                          : {
                              title: `Revoke the invitation for “${inv.name}”?`,
                              body: "Its link stops working at once. Nobody has joined with it, so no one loses access.",
                              yes: "Revoke",
                            }
                      }
                    />
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
