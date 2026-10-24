import Link from "next/link";
import { adminOnlyPage } from "@/components/AdminOnly";
import { SectionTabs } from "@/components/SectionNav";
import { Chip, EmptyState, PageHead, Panel, Stat, Table } from "@/components/ui";
import { getAgents } from "@/lib/agents";
import { agentHref } from "@/lib/agent-link";
import type { AgentSummary } from "@/lib/agents";

export const metadata = { title: "Agents" };

const access = (a: AgentSummary) => {
  if (!a.identity) return <Chip tone="warn">no identity</Chip>;
  if (a.identity.disabled) return <Chip tone="idle">disabled</Chip>;
  if (!a.identity.activeKeys) return <Chip tone="warn">no active key</Chip>;
  const soon = a.identity.keys.some((k) => k.status === "active" && k.expiringSoon);
  return <Chip tone={soon ? "warn" : "ok"}>{a.identity.activeKeys} key{a.identity.activeKeys === 1 ? "" : "s"}{soon ? ", one expiring" : ""}</Chip>;
};

export default async function Agents() {
  const gate = await adminOnlyPage("Agents");
  if (gate) return gate;
  const view = await getAgents();
  const working = view.agents.filter((a) => a.running > 0).length;
  const trouble = view.agents.filter((a) => a.failed > 0 || !a.identity || (!a.identity.disabled && !a.identity.activeKeys)).length;
  return (
    <>
      <SectionTabs section="logs" active="/agents" />
      <PageHead
        title="Agents"
        lede="Every agent that works with Brain, as one object: who it is in Access, what it is doing now, what it did and what it was given. Open one to pause it or change its keys."
        actions={<Link className="btn btn--primary btn--sm" href="/admin?new=agent#new-identity">Add an agent</Link>}
      />
      {view.agents.length > 0 && <div className="stats">
        <Stat label="Agents" value={String(view.agents.length)} note="known to Brain" />
        <div className={working ? "tone-ok" : undefined}><Stat label="Working now" value={String(working)} note="with a task in progress" /></div>
        <div className={trouble ? "tone-warn" : "tone-ok"}><Stat label="Need a look" value={String(trouble)} note="failed tasks or access gaps" /></div>
      </div>}
      {!view.accessReadable && <div className="callout tone-warn"><b>Access could not be read</b><span>Identities and keys are missing from this list until Brain answers again; tasks and packs are still shown.</span></div>}
      <Panel id="agents" title="All agents" flush>
        {view.agents.length === 0 ? (
          <EmptyState title="No agents yet" body="Create an identity for your first agent and paste its key into the agent's config. Its tasks and packs show up here as soon as it starts." action={<Link className="btn btn--primary" href="/admin?new=agent#new-identity">Add an agent</Link>} />
        ) : (
          <Table
            caption="Agents"
            rows={view.agents}
            rowKey={(a) => a.name}
            filter={{ search: (a) => `${a.name} ${a.note}`, noun: ["agent", "agents"] }}
            columns={[
              {
                head: "Agent",
                cell: (a) => (
                  <>
                    <Link className="link mono" href={agentHref(a.name) ?? "/agents"}>{a.name}</Link>
                    <span className="sub">{a.note}</span>
                  </>
                ),
              },
              { head: "Now", cell: (a) => a.identity?.disabled ? <Chip tone="idle">paused</Chip> : a.running ? <Chip tone="ok">{a.running} running</Chip> : <span className="text-faint">idle</span> },
              { head: "Tasks", cell: (a) => <span className="num">{a.completed} done{a.failed ? <span className="text-bad"> · {a.failed} failed</span> : ""}</span>, align: "right" },
              { head: "Packs", cell: (a) => <span className="num">{a.packs.length}</span>, align: "right" },
              { head: "Last seen", cell: (a) => <span className="num">{a.lastSeen}</span> },
              { head: "Access", cell: access },
            ]}
          />
        )}
      </Panel>
    </>
  );
}
