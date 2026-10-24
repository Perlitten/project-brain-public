// The dashboard inbox: what needs a person, and who is working. A server component so each row's button can carry its
// own server action; the rows themselves are plain links.
import Link from "next/link";
import { RunButton } from "@/components/act";
import { startReindex } from "@/lib/actions/jobs";
import { revokeCredential } from "@/lib/actions/admin";
import { agentHref } from "@/lib/agent-link";
import type { AgentNow, InboxItem } from "@/lib/inbox";
import { ListFrame } from "@/components/ListFrame";
import "./inbox.css";

function Action({ item }: { item: InboxItem }) {
  const a = item.action;
  if (!a) return null;
  if (a.kind === "reindex") {
    const path = a.repoPath;
    return <RunButton small variant="primary" action={async () => { "use server"; return startReindex({ repoPath: path }); }} label={a.label} title={`Re-index ${item.title}`} />;
  }
  if (a.kind === "revoke") {
    const id = a.credentialId;
    return (
      <RunButton small variant="danger" action={async () => { "use server"; return revokeCredential(id); }} label={a.label} title={`Revoke ${item.title}`}
        confirm={{ title: "Revoke this key?", body: "Anything still using it stops working at once. This cannot be undone.", yes: "Revoke key" }} />
    );
  }
  return <Link className={`btn btn--sm ${item.tone === "bad" ? "btn--primary" : "btn--neutral"}`} href={a.href}>{a.label}</Link>;
}

// Each half pages instead of growing: the Dashboard shows them as tabs.
const PAGE = 8;

export function InboxNeeds({ needs, complete, unavailable }: { needs: InboxItem[]; complete: boolean; unavailable: string[] }) {
  if (!needs.length) {
    return !complete
      ? <p className="ib-quiet"><b>No visible item needs you.</b> Brain could not check {unavailable.join(", ")}; retry before treating this as an all-clear.</p>
      : <p className="ib-quiet"><b>Nothing needs you.</b> Every run can land, no rule is broken, every tool and key is healthy.</p>;
  }
  return (
    <section className="ib-sec" aria-label="Needs you now">
      {!complete && <p className="ib-sec__note">Most blocking first. Some sources could not be checked.</p>}
      <ListFrame
        listClass="ib-items"
        pageSize={PAGE}
        searchable={needs.length > PAGE}
        placeholder="Search what needs you"
        noun={["item", "items"]}
        facet={{ label: "Urgency", values: [{ value: "bad", label: "Blocking" }, { value: "warn", label: "Look at" }, { value: "info", label: "For your info" }] }}
        meta={needs.map((n) => ({ q: `${n.title} ${n.why}`.toLowerCase(), f: n.tone }))}
        rows={needs.map((n) => (
          <li key={n.key} className={`ib-item tone-${n.tone}`}>
            <span className="ib-item__stage"><span className="ib-stage">{n.stage}</span></span>
            <div className="ib-item__body">
              <Link className="ib-item__title" href={n.href}>{n.title}</Link>
              <p className="ib-item__why">{n.why}</p>
            </div>
            <div className="ib-item__act"><Action item={n} /></div>
          </li>
        ))}
      />
    </section>
  );
}

export function InboxAgents({ agents }: { agents: AgentNow[] }) {
  return (
    <section className="ib-sec" aria-label="Agents right now">
      {agents.length ? (
        <ListFrame
          listClass="ib-items"
          pageSize={PAGE}
          searchable={agents.length > PAGE}
          placeholder="Search agents and tasks"
          noun={["agent", "agents"]}
          meta={agents.map((a) => ({ q: `${a.agent} ${a.task} ${a.where ?? ""}`.toLowerCase() }))}
          rows={agents.map((a) => {
            const who = agentHref(a.agent);
            return (
              <li key={a.key} className="ib-item ib-agent">
                <span className="ib-item__stage">
                  {who
                    ? <Link className="ib-agent__who" href={who}><span className="ib-agent__dot" aria-hidden="true" />{a.agent}</Link>
                    : <span className="ib-agent__who"><span className="ib-agent__dot" aria-hidden="true" />{a.agent}</span>}
                </span>
                <div className="ib-item__body">
                  <Link className="ib-item__title" href={a.href}>{a.task}</Link>
                  <p className="ib-item__why">
                    {a.where ? <>Working on <span className="mono">{a.where}</span></> : "In progress"}
                    {a.boardHref && <> · <Link className="link" href={a.boardHref}>board task</Link></>}
                  </p>
                </div>
                <span className="ib-agent__since">{a.since ? `since ${a.since}` : ""}</span>
              </li>
            );
          })}
        />
      ) : (
        <p className="ib-quiet">No agent is working right now. The next run shows up here the moment it starts.</p>
      )}
      <p className="ib-sec__note"><Link className="link" href="/agents">All agents →</Link></p>
    </section>
  );
}
