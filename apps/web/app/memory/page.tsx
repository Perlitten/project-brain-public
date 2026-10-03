import type { CSSProperties } from "react";
import { ListFrame } from "@/components/ListFrame";
import { PageTabs, pickTab } from "@/components/PageTabs";
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

const VIEWS = ["decisions", "rules"] as const;

export default async function Memory({ searchParams }: { searchParams: Promise<{ view?: string }> }) {
  const { view } = await searchParams;
  const tab = pickTab(VIEWS, view);
  const [decisions, rules] = await Promise.all([getDecisions(), getRules()]);
  const hitsTracked = rules.some((r) => r.hits30d !== undefined);
  const blocking = rules.filter((r) => r.severity === "block").length;
  return (
    <>
      <PageHead
        eyebrow="Decisions & rules"
        title="What your team decided, so agents remember it too"
        lede="Decisions explain why the code is the way it is. Rules are checks an agent’s change must pass before it is accepted."
      />
      <PageTabs
        label="Decisions or rules"
        path="/memory"
        current={tab}
        tabs={[
          { id: "decisions", label: "Decisions", badge: decisions.length },
          { id: "rules", label: "Rules", badge: blocking ? `${rules.length} · ${blocking} blocking` : rules.length },
        ]}
      />
      {tab === "decisions" ? (
        <section aria-label="Decisions">
          {decisions.length === 0 ? (
            <EmptyState title="No decisions recorded" body="Nobody has recorded a decision yet. Agents and people can record one through Brain." />
          ) : (
          <ListFrame
            listTag="div"
            listClass="cards cards--grid"
            noun={["decision", "decisions"]}
            meta={decisions.map((d) => ({ q: `${d.title} ${d.summary} ${d.scope} ${d.id}`.toLowerCase(), f: d.status }))}
            facet={{ label: "Status", values: (["accepted", "proposed", "superseded"] as const).map((v) => ({ value: v, label: statusWords[v] })) }}
            pageSize={20}
            rows={decisions.map((d, i) => (
            <article key={d.id} className={`card tone-${statusTone[d.status]}`} style={{ "--i": Math.min(i, 12) } as CSSProperties}>
              <div className="card__top">
                <Chip tone={statusTone[d.status]}>{statusWords[d.status]}</Chip>
                <span className="mono text-faint">{d.id}</span>
              </div>
              <h2 className="card__title">{d.title}</h2>
              <p className="card__body" title={d.summary}>{d.summary}</p>
              <p className="card__meta">
                <span className="mono">{d.scope}</span>
                <span>recorded {d.recordedAt}</span>
              </p>
            </article>
          ))}
          />
          )}
        </section>
      ) : (
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
            filter={{
              search: (r) => `${r.rule} ${r.id} ${r.scope}`,
              facet: { label: "If broken", of: (r) => r.severity, values: (["block", "warn", "advise"] as const).map((v) => ({ value: v, label: severity[v][1] })) },
              noun: ["rule", "rules"],
            }}
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
      )}
    </>
  );
}
