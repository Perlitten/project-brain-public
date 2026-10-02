import type { CSSProperties } from "react";
import { Chip, EmptyState, PageHead, Panel, Table } from "@/components/ui";
import { getDecisions, getRules } from "@/lib/data";
import type { Decision, Rule, Tone } from "@/lib/types";

export const metadata = { title: "Decisions & rules" };

const statusTone: Record<Decision["status"], Tone> = { accepted: "ok", proposed: "info", superseded: "idle" };
const statusWords: Record<Decision["status"], string> = { accepted: "agreed", proposed: "proposed", superseded: "replaced" };
const severity: Record<Rule["severity"], [Tone, string]> = {
  block: ["bad", "blocks the change"],
  warn: ["warn", "warns"],
  advise: ["idle", "suggests"],
};

export default async function Memory() {
  const [decisions, rules] = await Promise.all([getDecisions(), getRules()]);
  const hitsTracked = rules.some((r) => r.hits30d !== undefined);
  return (
    <>
      <PageHead
        eyebrow="Decisions & rules"
        title="What your team decided, so agents remember it too"
        lede="Decisions explain why the code is the way it is. Rules are checks an agent’s change must pass before it is accepted."
      />
      <div className="grid-halves">
        <section className="cards" aria-label="Decisions">
          {decisions.length === 0 && (
            <EmptyState title="No decisions recorded" body="Nobody has recorded a decision yet. Agents and people can record one through Brain." />
          )}
          {decisions.map((d, i) => (
            <article key={d.id} className={`card tone-${statusTone[d.status]}`} style={{ "--i": i } as CSSProperties}>
              <div className="card__top">
                <Chip tone={statusTone[d.status]}>{statusWords[d.status]}</Chip>
                <span className="mono text-faint">{d.id}</span>
              </div>
              <h2 className="card__title">{d.title}</h2>
              <p className="card__body">{d.summary}</p>
              <p className="card__meta">
                <span className="mono">{d.scope}</span>
                <span>recorded {d.recordedAt}</span>
              </p>
            </article>
          ))}
        </section>
        <Panel id="rules" title="Rules agents must follow" desc={hitsTracked ? "“Caught” counts problems each rule stopped in the last 30 days." : "Brain doesn’t count how often each rule fires yet."}
          flush
        >
          {rules.length === 0 ? (
            <EmptyState title="No rules yet" body="No active rules are recorded. Add one so agents’ changes are checked against it." />
          ) : (
          <Table
            caption="Rules"
            rows={rules}
            rowKey={(r) => r.id}
            columns={[
              {
                head: "Rule",
                cell: (r) => (
                  <>
                    <span>{r.rule}</span>
                    <span className="sub mono">
                      {r.id} · {r.scope}
                    </span>
                  </>
                ),
              },
              { head: "If broken", cell: (r) => <Chip tone={severity[r.severity][0]}>{severity[r.severity][1]}</Chip> },
              { head: "Caught", cell: (r) => r.hits30d ?? "—", align: "right" },
            ]}
          />
          )}
        </Panel>
      </div>
    </>
  );
}
