"use client";
// Access, made of controls: create an identity with its first key, pick its
// permissions from presets or one by one, and open any identity to see every
// key it ever had — mint a new one, revoke an old one, disable or enable it.
// New keys appear once, in the shared KeyDialog.

import "./access.css";
import { useMemo, useState, type CSSProperties } from "react";
import { Dialog, LockNote, RunButton, useAction } from "@/components/act";
import { Icon } from "@/components/Icon";
import { KeyDialog } from "@/components/KeyDialog";
import { ListFrame } from "@/components/ListFrame";
import { Chip } from "@/components/ui";
import { createAgentKey, disablePrincipal, enablePrincipal, mintKey, revokeCredential, type NewKey } from "@/lib/actions/admin";
import { EXPIRY_CHOICES, KNOWN_SCOPES, SCOPE_GROUPS, SCOPE_PRESETS, TEAM_SCOPES, sameScopes, isNamedScope } from "@/lib/scopes";
import type { AccessKey, Identity, Tone } from "@/lib/types";

type Kind = Identity["kind"];

export const KIND_WORD: Record<Kind, string> = { agent: "AI agent", service: "integration", human: "person" };
const KIND_PRESET: Record<Kind, string> = { agent: "agent", service: "integration", human: "read" };
const presetScopes = (id: string) => SCOPE_PRESETS.find((p) => p.id === id)?.scopes ?? [];

/** Where a new key's agent config points. */
export interface AgentTarget {
  apiUrl: string;
  repoPath?: string;
  httpMcp: boolean;
}

const KEY_HINT = (
  <>
    For an agent, paste one of the configs above — it connects by URL, nothing to install. For an integration, send the key in the{" "}
    <code>X-API-Key</code> header.
  </>
);

// ---------------------------------------------------------------------------
// Permissions: presets first, every scope one click further

function ScopePicker({ value, onChange }: { value: string[]; onChange: (next: string[]) => void }) {
  const has = (s: string) => value.includes(s);
  const toggle = (s: string, on: boolean) => onChange(on ? [...new Set([...value, s])].sort() : value.filter((x) => x !== s));
  const extra = value.filter((s) => !KNOWN_SCOPES.has(s));
  const preset = SCOPE_PRESETS.find((p) => sameScopes(p.scopes, value));
  return (
    <div className="field">
      <span className="field__label">
        Permissions <em>{preset ? preset.label : value.length ? "custom" : "none picked"}</em>
      </span>
      <div className="presets" role="group" aria-label="Permission presets">
        {SCOPE_PRESETS.map((p) => (
          <button key={p.id} type="button" className="preset" aria-pressed={preset?.id === p.id} onClick={() => onChange([...p.scopes].sort())}>
            <b>{p.label}</b>
            <small>{p.note}</small>
          </button>
        ))}
      </div>
      <span className="chips" aria-label="Picked permissions">
        {value.length ? (
          value.map((s) => (
            <Chip key={s} tone="idle" dot={false}>
              {s}
            </Chip>
          ))
        ) : (
          <span className="field__hint field__hint--bad">Pick at least one permission.</span>
        )}
      </span>
      <details className="scope-pick">
        <summary>Choose permissions one by one</summary>
        <p className="field__hint">Write includes read in the same area.</p>
        {SCOPE_GROUPS.map((g) => (
          <fieldset key={g.label} className="scope-pick__group">
            <legend>{g.label}</legend>
            {g.areas.map((a) => {
              const w = `${a.area}:write`;
              const r = `${a.area}:read`;
              return (
                <div key={a.area} className="scope-pick__row">
                  <span className="scope-pick__name">
                    <b>{a.label}</b>
                    <small>{a.about}</small>
                  </span>
                  <label className="check" title={a.read && has(w) ? "Included with write" : undefined}>
                    <input type="checkbox" checked={has(r) || has(w)} disabled={!a.read || has(w)} onChange={(e) => toggle(r, e.target.checked)} />
                    read
                  </label>
                  {a.write ? (
                    <label className="check">
                      <input type="checkbox" checked={has(w)} onChange={(e) => toggle(w, e.target.checked)} />
                      write
                    </label>
                  ) : (
                    <span className="scope-pick__none">read only</span>
                  )}
                </div>
              );
            })}
          </fieldset>
        ))}
        <fieldset className="scope-pick__group">
          <legend>Team</legend>
          <p className="field__hint">These scopes do not create membership or a project role. Brain also checks the person’s live membership and project permissions; you can only mint scopes your current credential holds.</p>
          {TEAM_SCOPES.map((s) => <label key={s} className="check"><input type="checkbox" checked={has(s)} onChange={(e) => toggle(s, e.target.checked)} /><span className="mono">{s}</span></label>)}
        </fieldset>
        {extra.length > 0 && (
          <fieldset className="scope-pick__group">
            <legend>Other</legend>
            {extra.map((s) => (
              <label key={s} className="check">
                <input type="checkbox" checked onChange={() => toggle(s, false)} />
                <span className="mono">{s}</span>
              </label>
            ))}
          </fieldset>
        )}
      </details>
    </div>
  );
}

function ExpirySelect({ value, onChange }: { value: number; onChange: (days: number) => void }) {
  return (
    <label className="field">
      <span className="field__label">Expires</span>
      <select className="select" value={value} onChange={(e) => onChange(Number(e.target.value))}>
        {EXPIRY_CHOICES.map((c) => (
          <option key={c.days} value={c.days}>
            {c.label}
          </option>
        ))}
      </select>
      <span className="field__hint">An expired key stops working on its own.</span>
    </label>
  );
}

// ---------------------------------------------------------------------------
// New identity with its first key

export function NewIdentityForm({ target }: { target: AgentTarget }) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState<Kind>("agent");
  const [scopes, setScopes] = useState<string[]>(() => [...presetScopes("agent")].sort());
  const [ttl, setTtl] = useState(90);
  const [key, setKey] = useState<NewKey | null>(null);
  const { run, pending, mode } = useAction(createAgentKey, { title: "New identity" });

  // Switching the kind swaps in its usual preset — unless you already changed the permissions.
  const pickKind = (k: Kind) => {
    if (sameScopes(scopes, presetScopes(KIND_PRESET[kind]))) setScopes([...presetScopes(KIND_PRESET[k])].sort());
    setKind(k);
  };

  return (
    <>
      <form
        className="form form--wide"
        onSubmit={async (e) => {
          e.preventDefault();
          const res = await run(name, scopes, { kind, ttlDays: ttl || undefined });
          if (res.ok && res.data) {
            setKey(res.data);
            setName("");
          }
        }}
        autoComplete="off"
      >
        <div className="form__row">
          <label className="field">
            <span className="field__label">Name</span>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="claude-laptop" maxLength={120} spellCheck={false} />
            <span className="field__hint">Something you’ll recognise later, e.g. the machine or the tool.</span>
          </label>
          <div className="field">
            <span className="field__label">Kind</span>
            <div className="seg seg--fill" role="group" aria-label="Kind">
              {(Object.keys(KIND_WORD) as Kind[]).map((k) => (
                <button key={k} type="button" aria-pressed={kind === k} onClick={() => pickKind(k)}>
                  {KIND_WORD[k]}
                </button>
              ))}
            </div>
            <span className="field__hint">Picks the starting permissions below; you can change them.</span>
          </div>
          <ExpirySelect value={ttl} onChange={setTtl} />
        </div>
        <ScopePicker value={scopes} onChange={setScopes} />
        <div className="btn-row">
          <button type="submit" className="btn btn--primary" disabled={pending || mode !== "on" || !name.trim() || scopes.length === 0 || scopes.some(scope => !isNamedScope(scope))}>
            {pending ? <span className="spin" aria-hidden="true" /> : <Icon name="key" size={15} />}
            Create identity and key
          </button>
        </div>
        <LockNote />
      </form>
      <KeyDialog k={key} apiUrl={target.apiUrl} repoPath={target.repoPath} httpMcp={target.httpMcp} onClose={() => setKey(null)} hint={target.httpMcp ? KEY_HINT : undefined} />
    </>
  );
}

// ---------------------------------------------------------------------------
// The list: status filter here, search and kind filter in the ListFrame

type StatusFilter = "all" | "active" | "disabled";

export function IdentityList({ identities, target }: { identities: Identity[]; target: AgentTarget }) {
  const [status, setStatus] = useState<StatusFilter>("all");
  const counts = { all: identities.length, active: identities.filter((i) => !i.disabled).length, disabled: identities.filter((i) => i.disabled).length };
  const shown = useMemo(() => identities.filter((i) => status === "all" || (status === "disabled") === i.disabled), [identities, status]);
  return (
    <div className="access-list">
      {identities.some(i => i.current === undefined) && <div className="callout tone-warn"><b>Dashboard credential not identified</b><span>This API does not report which identity and key serve this dashboard. Destructive controls are disabled until the server supports credential identification; names are not proof of identity.</span></div>}
      {counts.disabled > 0 && (
        <div className="access-list__status">
          <div className="seg" role="group" aria-label="Filter by status">
            {(["all", "active", "disabled"] as StatusFilter[]).map((s) => (
              <button key={s} type="button" aria-pressed={status === s} onClick={() => setStatus(s)}>
                {s === "all" ? "All" : s === "active" ? "Enabled" : "Disabled"} <span className="lf__n">{counts[s]}</span>
              </button>
            ))}
          </div>
        </div>
      )}
      <ListFrame
        rows={shown.map((i, n) => (
          <IdentityRow key={i.id} identity={i} index={n} target={target} />
        ))}
        meta={shown.map((i) => ({
          q: `${i.name} ${KIND_WORD[i.kind]} ${i.kind} ${i.scopes.join(" ")} ${i.disabled ? "disabled" : ""}`.toLowerCase(),
          f: i.kind,
        }))}
        facet={{ label: "Kind", values: (Object.keys(KIND_WORD) as Kind[]).map((k) => ({ value: k, label: KIND_WORD[k] })) }}
        noun={["identity", "identities"]}
        listClass="idents"
      />
    </div>
  );
}

function keyTone(k: AccessKey, disabled: boolean): [Tone, string] {
  if (k.status === "revoked") return ["idle", "revoked"];
  if (k.status === "expired") return ["idle", "expired"];
  if (disabled) return ["idle", "paused"];
  if (k.expiringSoon) return ["warn", "expires soon"];
  return ["ok", "active"];
}

function IdentityRow({ identity: i, index, target }: { identity: Identity; index: number; target: AgentTarget }) {
  const [open, setOpen] = useState(false);
  const [minting, setMinting] = useState<AccessKey | "new" | null>(null);
  const [key, setKey] = useState<NewKey | null>(null);
  const newest = i.keys.find((k) => k.status === "active");
  const panel = `ident-${i.id}`;
  return (
    <li className={`ident${open ? " is-open" : ""}${i.disabled ? " is-disabled" : ""}`} style={{ "--i": Math.min(index, 12) } as CSSProperties}>
      <button type="button" className="ident__head" aria-expanded={open} aria-controls={panel} onClick={() => setOpen((v) => !v)}>
        <span className="ident__name">
          <span className="mono">{i.name}</span>
          {i.current && <Chip tone="info">This dashboard</Chip>}
          <span className="sub">
            {KIND_WORD[i.kind]} · since {i.createdAt} · last seen {i.lastSeen}
          </span>
        </span>
        <span className="chips ident__scopes">
          {i.disabled && <Chip tone="bad">disabled</Chip>}
          {i.scopes.map((s) => (
            <Chip key={s} tone="idle" dot={false}>
              {s}
            </Chip>
          ))}
        </span>
        <span className="ident__count num">
          {i.activeKeys} active key{i.activeKeys === 1 ? "" : "s"}
        </span>
        <span className="ident__toggle">
          {open ? "Hide" : "Manage"}
          <Icon name="arrow" size={14} />
        </span>
      </button>

      {open && (
        <div className="ident__panel" id={panel}>
          <div className="btn-row">
            <button type="button" className="btn btn--primary btn--sm" onClick={() => setMinting(i.current ? i.keys.find(k => k.current) ?? "new" : "new")} disabled={i.disabled}>
              <Icon name="key" size={15} />
              {i.current ? "Rotate dashboard key" : "New key"}
            </button>
            {i.disabled ? (
              <RunButton
                small
                action={() => enablePrincipal(i.id)}
                label="Enable"
                title={`Enable “${i.name}”`}
                confirm={{
                  title: `Enable “${i.name}”?`,
                  body: "Its keys that aren’t revoked or expired start working again right away.",
                  yes: "Enable",
                }}
              />
            ) : !i.current && (
              <RunButton
                small
                variant="danger"
                action={() => disablePrincipal(i.id)}
                disabled={i.current !== false}
                disabledReason="This identity serves the dashboard. Switch to another administrative identity before disabling it."
                label="Disable"
                title={`Disable “${i.name}”`}
                confirm={{
                  title: `Disable “${i.name}”?`,
                  body: "None of its keys work while it’s disabled. Nothing is deleted — enable it again any time.",
                  yes: "Disable",
                }}
              />
            )}
          </div>
          {i.current && <div className="callout tone-warn"><b>This dashboard uses this identity</b><span>Rotation creates a replacement without revoking the current key. Save the new key, ask the deployment owner to update BRAIN_API_KEY in Vercel and redeploy, then verify sign-in with another administrative credential before revoking the old one. This page does not change deployment settings.</span></div>}
          <p className="field__hint">
            <b>New key</b> adds a key with the permissions you pick — to rotate, create one, switch the agent to it, then revoke the old one.{" "}
            <b>Revoke</b> stops one key for good. <b>Disable</b> pauses every key of this identity until you enable it again.
          </p>

          {i.keys.length === 0 ? (
            <p className="field__hint">No keys yet.</p>
          ) : (
            <div className="table-wrap">
              <table className="table keys">
                <caption className="sr-only">Keys of {i.name}</caption>
                <thead>
                  <tr>
                    <th scope="col">Key</th>
                    <th scope="col">Allowed to</th>
                    <th scope="col">Created</th>
                    <th scope="col">Expires</th>
                    <th scope="col">Last used</th>
                    <th scope="col">Status</th>
                    <th scope="col" className="r">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
              {i.keys.map((k) => {
                    const [tone, word] = keyTone(k, i.disabled);
                    return (
                      <tr key={k.id} className={k.status === "active" ? undefined : "keys__gone"}>
                        <td className="num">#{k.id} {k.current && <Chip tone="info">This dashboard</Chip>}</td>
                        <td>
                          <span className="chips">
                            {k.scopes.map((s) => (
                              <Chip key={s} tone={s === "*" ? "warn" : "idle"} dot={false}>
                                {s === "*" ? "everything" : s}
                              </Chip>
                            ))}
                          </span>
                        </td>
                        <td className="num">{k.created}</td>
                        <td className="num">{k.expires}</td>
                        <td className="num">{k.lastUsed}</td>
                        <td>
                          <Chip tone={tone}>{word}</Chip>
                        </td>
                        <td className="r">
                          {k.status === "active" && (k.current ? <button type="button" className="btn btn--neutral btn--sm" onClick={() => setMinting(k)} disabled={i.disabled}>Rotate</button> : (
                            <RunButton
                              small
                              variant="danger"
                              action={() => revokeCredential(k.id)}
                               disabled={k.current !== false}
                               disabledReason="The API has not identified the dashboard key. Update the server before revoking credentials. Create a replacement, switch the dashboard to it and verify access before revoking this key."
                              label="Revoke"
                              title={`Revoke key #${k.id}`}
                              confirm={{
                                title: `Revoke key #${k.id} of “${i.name}”?`,
                                body: "Anything using this key is locked out right away. This can’t be undone — create a new key if access is still needed.",
                                yes: "Revoke key",
                              }}
                            />
                          ))}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <Dialog open={minting !== null} onClose={() => setMinting(null)} title={`${i.current ? "Rotate dashboard key" : "New key"} for “${i.name}”`}>
        <MintForm
          identity={i}
          key={typeof minting === "object" && minting ? minting.id : "new"}
          initial={(typeof minting === "object" && minting ? minting : newest)?.scopes ?? []}
          onCancel={() => setMinting(null)}
          onMinted={(k) => {
            setMinting(null);
            setKey({ ...k, name: i.name });
          }}
        />
      </Dialog>
      <KeyDialog k={key} apiUrl={target.apiUrl} repoPath={target.repoPath} httpMcp={target.httpMcp} onClose={() => setKey(null)} hint={target.httpMcp ? KEY_HINT : undefined} />
    </li>
  );
}

// Lives inside the dialog, so every opening starts from the newest key's permissions.
function MintForm({ identity, initial, onCancel, onMinted }: { identity: Identity; initial: string[]; onCancel: () => void; onMinted: (k: NewKey) => void }) {
  const [scopes, setScopes] = useState<string[]>(() => (initial.length ? [...initial].sort() : [...presetScopes(KIND_PRESET[identity.kind])].sort()));
  const [ttl, setTtl] = useState(90);
  const { run, pending, mode } = useAction(mintKey, { title: "New key" });
  const unsupported = scopes.filter(scope => !isNamedScope(scope));
  return (
    <form
      className="form form--wide"
      onSubmit={async (e) => {
        e.preventDefault();
        const res = await run(identity.id, scopes, ttl || undefined);
        if (res.ok && res.data) onMinted(res.data);
      }}
    >
      {!!unsupported.length && <p className="field__hint field__hint--bad" role="alert">This key includes permissions the API cannot mint: {unsupported.join(", ")}. Choose explicit replacement permissions below before creating a key. The existing key stays active.</p>}
      <p className="dialog__text">
        {initial.length ? "Starts from the selected key’s permissions." : "Starts from the usual permissions for this kind."} Its other keys keep working until you
        revoke them.
      </p>
      <ScopePicker value={scopes} onChange={setScopes} />
      <ExpirySelect value={ttl} onChange={setTtl} />
      <LockNote />
      <div className="dialog__actions">
        <button type="button" className="btn btn--neutral" onClick={onCancel}>
          Cancel
        </button>
        <button type="submit" className="btn btn--primary" disabled={pending || mode !== "on" || scopes.length === 0 || scopes.some(scope => !isNamedScope(scope))}>
          {pending ? <span className="spin" aria-hidden="true" /> : <Icon name="key" size={15} />}
          Create key
        </button>
      </div>
    </form>
  );
}
