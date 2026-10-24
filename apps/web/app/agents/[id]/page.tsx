import Link from "next/link";
import { notFound } from "next/navigation";
import { adminOnlyPage } from "@/components/AdminOnly";
import { RunButton } from "@/components/act";
import { SectionTabs } from "@/components/SectionNav";
import { Chip, EmptyState, PageHead, Panel, Stat, Table, fmt } from "@/components/ui";
import { disablePrincipal, enablePrincipal, revokeCredential } from "@/lib/actions/admin";
import { getAgent } from "@/lib/agents";
import { scopeWords } from "@/lib/scopes";
import { packForRun, runForPack } from "@/lib/task-links";
import { TaskResult } from "@/components/TaskActions";
import type { AccessKey, Tone } from "@/lib/types";

export async function generateMetadata({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return { title: `Agent ${decodeURIComponent(id)}` };
}

const RUNNING = new Set(["running", "retrying", "queued"]);

function keyState(k: AccessKey, disabled: boolean): [Tone, string] {
  if (k.status === "revoked") return ["idle", "revoked"];
  if (k.status === "expired") return ["idle", "expired"];
  if (disabled) return ["idle", "paused"];
  if (k.expiringSoon) return ["warn", `expires ${k.expires}`];
  if (k.neverUsed) return ["info", "never used"];
  return ["ok", "active"];
}

export default async function Agent({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const name = decodeURIComponent(id);
  const gate = await adminOnlyPage(`Agent ${name}`);
  if (gate) return gate;
  const agent = await getAgent(name);
  if (!agent) notFound();

  const ident = agent.identity;
  const running = agent.runs.filter((r) => RUNNING.has(r.status));
  const status = ident?.disabled
    ? { tone: "idle" as Tone, text: "Disabled: none of its keys work until you enable it again." }
    : !ident
      ? { tone: "warn" as Tone, text: "No identity in Access. Brain knows this name from task reports only and cannot pause it." }
      : !ident.activeKeys
        ? { tone: "warn" as Tone, text: "No active key: the agent cannot reach Brain until you mint one." }
        : running.length
          ? { tone: "ok" as Tone, text: `Working on ${running.length === 1 ? `“${running[0].task}”` : `${running.length} tasks`} right now.` }
          : agent.failed
            ? { tone: "warn" as Tone, text: `Idle. ${agent.failed} of its recent tasks ended badly; see below.` }
            : { tone: "ok" as Tone, text: `Idle. Last seen ${agent.lastSeen}.` };

  const identId = ident?.id;
  const actions = ident ? (
    <>
      {!ident.disabled && <Link className="btn btn--neutral btn--sm" href={`/admin?new=key&for=${ident.id}#identity-${ident.id}`}>New key</Link>}
      {ident.disabled ? (
        <RunButton small action={async () => { "use server"; return enablePrincipal(identId!); }} label="Enable agent" title={`Enable ${agent.name}`}
          confirm={{ title: `Enable “${agent.name}”?`, body: "Its keys that aren’t revoked or expired start working again right away.", yes: "Enable" }} />
      ) : (
        <RunButton small variant="danger" action={async () => { "use server"; return disablePrincipal(identId!); }} label="Disable agent" title={`Disable ${agent.name}`}
          disabled={ident.current !== false} disabledReason="This identity serves the dashboard, or the API has not said which one does. Switch to another administrative identity first."
          confirm={{ title: `Disable “${agent.name}”?`, body: "Every key of this agent stops working at once; running tasks lose access to Brain. Nothing is deleted — enable it again any time.", yes: "Disable" }} />
      )}
    </>
  ) : (
    <Link className="btn btn--primary btn--sm" href={`/admin?new=agent&name=${encodeURIComponent(agent.name)}#new-identity`}>Give it an identity</Link>
  );

  return (
    <>
      <SectionTabs section="logs" active="/agents" />
      <PageHead
        eyebrow="Agent"
        title={agent.name}
        status={status}
        actions={actions}
        lede={<><Link className="link" href="/agents">All agents</Link> · tasks in <Link className="link" href={`/runs?q=${encodeURIComponent(agent.name)}`}>Agent tasks</Link> · packs in <Link className="link" href={`/packs?q=${encodeURIComponent(agent.name)}`}>Context packs</Link>{ident && <> · keys in <Link className="link" href={`/admin#identity-${ident.id}`}>Access</Link></>}</>}
      />

      <div className="stats">
        <div className={running.length ? "tone-ok" : undefined}><Stat label="Running now" value={String(running.length)} note={running.length ? "tasks in progress" : "nothing in progress"} /></div>
        <Stat label="Tasks" value={String(agent.runs.length)} note={`${agent.completed} completed`} />
        <div className={agent.failed ? "tone-warn" : "tone-ok"}><Stat label="Ended badly" value={String(agent.failed)} note="failed or no end reported" /></div>
        <Stat label="Context packs" value={String(agent.packs.length)} note={agent.packs.length ? `last ${agent.packs[0].createdAt}` : "none yet"} />
        <Stat label="Tokens" value={fmt(agent.runs.reduce((n, r) => n + (r.tokens ?? 0), 0))} note="across recorded tasks" />
      </div>

      <Panel id="identity" title="Identity and keys" desc={ident ? `${ident.kind === "agent" ? "AI agent" : ident.kind} in Access since ${ident.createdAt}. Revoking a key locks out whatever uses it at once.` : "This name is not in Access."}>
        {!ident ? (
          <EmptyState title="No identity" body="Brain sees this name only because an agent reported tasks under it. Create an identity to give it its own key, permissions and an off switch." action={<Link className="btn btn--primary" href={`/admin?new=agent&name=${encodeURIComponent(agent.name)}#new-identity`}>Create identity</Link>} />
        ) : (
          <>
            <p className="sub" style={{ marginBottom: "var(--s-3)" }}>
              Allowed to: {ident.scopes.length ? ident.scopes.map(scopeWords).join(" · ") : "nothing yet"}.
            </p>
            {ident.keys.length === 0 ? <p className="sub">No keys yet.</p> : (
              <Table
                caption={`Keys of ${agent.name}`}
                rows={ident.keys}
                rowKey={(k) => String(k.id)}
                columns={[
                  { head: "Key", cell: (k) => <span className="num">#{k.id}</span> },
                  { head: "Allowed to", cell: (k) => <span className="sub">{k.scopes.map(scopeWords).join(" · ")}</span> },
                  { head: "Created", cell: (k) => <span className="num">{k.created}</span> },
                  { head: "Last used", cell: (k) => <span className="num">{k.lastUsed}</span> },
                  { head: "State", cell: (k) => { const [tone, word] = keyState(k, ident.disabled); return <Chip tone={tone}>{word}</Chip>; } },
                  {
                    head: "", align: "right",
                    cell: (k) => {
                      if (k.status !== "active") return null;
                      const kid = k.id;
                      return (
                        <RunButton small variant="danger" action={async () => { "use server"; return revokeCredential(kid); }} label="Revoke" title={`Revoke key #${k.id}`}
                          disabled={k.current !== false} disabledReason="The API has not said which key serves the dashboard; update the server before revoking."
                          confirm={{ title: `Revoke key #${k.id} of “${agent.name}”?`, body: "Anything using this key is locked out right away. This can’t be undone — mint a new key if access is still needed.", yes: "Revoke key" }} />
                      );
                    },
                  },
                ]}
              />
            )}
          </>
        )}
      </Panel>

      <Panel id="tasks" title="Tasks" desc="What this agent told Brain it was doing, newest first. A task nobody closed is marked timed out; the work may still have finished." flush>
        {agent.runs.length === 0 ? <EmptyState title="No tasks recorded" body="This agent has not reported a task to Brain yet." /> : (
          <Table
            caption={`Tasks of ${agent.name}`}
            rows={agent.runs}
            rowKey={(r) => r.id}
            filter={{ search: (r) => `${r.task} ${r.id} ${r.repo ?? ""} ${r.status}`, facet: { label: "Result", of: (r) => r.status, values: [{ value: "running", label: "running" }, { value: "completed", label: "completed" }, { value: "failed", label: "failed" }, { value: "timed_out", label: "no end reported" }] }, noun: ["task", "tasks"] }}
            columns={[
              {
                head: "Task",
                cell: (r) => (
                  <>
                    <Link className="link" href={`/runs?q=${encodeURIComponent(r.id)}`}>{r.task}</Link>
                    <span className="sub mono">{[r.repo, r.id.length > 12 ? r.id.slice(0, 8) : r.id].filter(Boolean).join(" · ")}</span>
                  </>
                ),
              },
              {
                head: "Context pack",
                cell: (r) => {
                  if (r.packHit === undefined) return <span className="text-faint">—</span>;
                  if (!r.packHit) return <Chip tone="warn">none</Chip>;
                  const pack = packForRun(r, agent.packs);
                  return pack ? <Link className="link" href={`/packs/${encodeURIComponent(pack.id)}?back=${encodeURIComponent(`/agents/${encodeURIComponent(agent.name)}`)}`}>pack #{pack.id}</Link> : <Chip tone="ok">yes</Chip>;
                },
              },
              { head: "Started", cell: (r) => <span className="num">{r.startedAt}</span> },
              { head: "Took", cell: (r) => r.duration, align: "right" },
              { head: "Result", cell: (r) => <TaskResult run={r} /> },
            ]}
          />
        )}
      </Panel>

      <Panel id="packs" title="Context packs given" desc="What Brain handed this agent for each task: files, rules and decisions within a token budget." flush>
        {agent.packs.length === 0 ? <EmptyState title="No packs yet" body="This agent has not asked Brain for context. Packs appear here the moment it does." /> : (
          <Table
            caption={`Context packs for ${agent.name}`}
            rows={agent.packs}
            rowKey={(p) => p.id}
            columns={[
              { head: "Task", cell: (p) => <><Link className="link" href={`/packs/${encodeURIComponent(p.id)}?back=${encodeURIComponent(`/agents/${encodeURIComponent(agent.name)}`)}`}>{p.task}</Link><span className="sub mono">#{p.id}{p.repo ? ` · ${p.repo}` : ""}{(() => { const run = runForPack(p, agent.runs); return run ? <> · task <Link className="link" href={`/runs?q=${encodeURIComponent(run.id)}`}>{run.id.length > 12 ? run.id.slice(0, 8) : run.id}</Link></> : null; })()}</span></> },
              { head: "Built", cell: (p) => <span className="num">{p.createdAt}</span> },
              { head: "Files", cell: (p) => fmt(p.files), align: "right" },
              { head: "Tokens", cell: (p) => fmt(p.tokens), align: "right" },
              { head: "Code", cell: (p) => p.stale === null ? <span className="text-faint">unknown</span> : p.stale ? <Chip tone="warn">older</Chip> : <Chip tone="ok">current</Chip> },
            ]}
          />
        )}
      </Panel>
    </>
  );
}
