// The dashboard inbox. A server component so each row's button can carry its
// own server action; the rows themselves are plain links.
import Link from "next/link";
import { RunButton } from "@/components/act";
import { startReindex } from "@/lib/actions/jobs";
import { revokeCredential } from "@/lib/actions/admin";
import { agentHref } from "@/lib/agent-link";
import type { AgentNow, InboxItem } from "@/lib/inbox";
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

export function Inbox({ needs, agents }: { needs: InboxItem[]; agents: AgentNow[] }) {
  return (
    <div className="ib">
      <section className="ib-sec" aria-labelledby="ib-needs">
        <div className="ib-sec__head">
          <h2 id="ib-needs">Needs you now</h2>
          {needs.length > 0 && <span className="num">{needs.length}</span>}
          <span className="ib-sec__note">{needs.length ? "Most blocking first. Everything else runs on its own." : ""}</span>
        </div>
        {needs.length ? (
          <ul className="ib-items">
            {needs.map((n) => (
              <li key={n.key} className={`ib-item tone-${n.tone}`}>
                <span className="ib-item__stage"><span className="ib-stage">{n.stage}</span></span>
                <div className="ib-item__body">
                  <Link className="ib-item__title" href={n.href}>{n.title}</Link>
                  <p className="ib-item__why">{n.why}</p>
                </div>
                <div className="ib-item__act"><Action item={n} /></div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="ib-quiet"><b>Nothing needs you.</b> Every run can land, no rule is broken, every tool and key is healthy.</p>
        )}
      </section>

      <section className="ib-sec" aria-labelledby="ib-agents">
        <div className="ib-sec__head">
          <h2 id="ib-agents">Agents right now</h2>
          {agents.length > 0 && <span className="num">{agents.length}</span>}
          <Link className="ib-sec__more link" href="/agents">All agents →</Link>
        </div>
        {agents.length ? (
          <ul className="ib-items">
            {agents.map((a) => {
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
          </ul>
        ) : (
          <p className="ib-quiet">No agent is working right now. The next run shows up here the moment it starts.</p>
        )}
      </section>
    </div>
  );
}
