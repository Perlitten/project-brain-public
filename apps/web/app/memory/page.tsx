import type { CSSProperties } from "react";
import { SectionNav } from "@/components/SectionNav";
import { ListFrame } from "@/components/ListFrame";
import { PageTabs, pickTab } from "@/components/PageTabs";
import { Chip, EmptyState, PageHead, Panel, Table } from "@/components/ui";
import { getPagedDecisions, getPagedRules } from "@/lib/data";
import { getListQuery } from "@/lib/list-query";
import type { Decision, Rule, Tone } from "@/lib/types";

export const metadata = { title: "Decisions & rules" };

const statusTone: Record<Decision["status"], Tone> = { accepted: "ok", proposed: "info", superseded: "idle", validated_unmerged: "info" };
const statusWords: Record<Decision["status"], string> = { accepted: "agreed", proposed: "proposed", superseded: "replaced", validated_unmerged: "checked, not merged" };
const severity: Record<Rule["severity"], [Tone, string]> = {
  block: ["bad", "blocks the change"],
  warn: ["warn", "warns"],
  advise: ["idle", "suggests"],
};

const VIEWS = ["decisions", "rules"] as const;

export default async function Memory({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const view = typeof params.view === "string" ? params.view : undefined;
  const tab = pickTab(VIEWS, view);
  const query = getListQuery(params);
  const hasQuery = Boolean(params.q || params.status || params.page || params.page_size);
  const repo = typeof params.repo === "string" ? params.repo : undefined;
  const [decisionsResult, rulesResult] = await Promise.all([
    getPagedDecisions(repo, tab === "decisions" ? query : {}),
    getPagedRules(repo, tab === "rules" ? query : {}),
  ]);
  const decisions = decisionsResult.items;
  const rules = rulesResult.items;
  const hitsTracked = rules.some((r) => r.hits30d !== undefined);
  const blocking = rules.filter((r) => r.severity === "block").length;
  return (
    <>
      <PageHead
        title="Memory"
        lede={`Decisions and rules your team recorded${repo ? " for this project" : " across all projects"}. Agents get the relevant ones with their context.`}
      />
      <SectionNav label="Memory views" items={[{ href: "/memory", label: "Decisions & rules", active: true }, { href: "/packs", label: "Context packs" }]} />
        <PageTabs
        label="Decisions or rules"
        path="/memory"
          current={tab}
          params={{ repo }}
        tabs={[
          { id: "decisions", label: "Decisions", badge: decisionsResult.paging.total },
          { id: "rules", label: "Rules", badge: rulesResult.paging.total },
        ]}
      />
      {tab === "decisions" ? (
        <section aria-label="Decisions">
          {decisions.length === 0 && !hasQuery ? (
            <EmptyState title="No decisions recorded" body="Nobody has recorded a decision yet. Agents and people can record one through Brain." />
          ) : (
          <ListFrame
            listTag="div"
            listClass="cards cards--grid"
            noun={["decision", "decisions"]}
            meta={decisions.map((d) => ({ q: `${d.title} ${d.summary} ${d.scope} ${d.id}`.toLowerCase(), f: d.status }))}
            facet={{ label: "Status", values: (["accepted", "proposed", "superseded", "validated_unmerged"] as const).map((v) => ({ value: v, label: statusWords[v] })) }}
            pageSize={20}
            paging={decisionsResult.paging}
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
        <Panel id="rules" title="Rules agents must follow" desc={`${blocking} blocking rule${blocking === 1 ? "" : "s"} on this page. ${hitsTracked ? "“Caught” counts problems each rule stopped in the last 30 days." : "Brain doesn’t count how often each rule fires yet."}`}
          flush
        >
          {rules.length === 0 && !hasQuery ? (
            <EmptyState title="No rules yet" body="No active rules are recorded. Add one so agents’ changes are checked against it." />
          ) : (
          <Table
            caption="Rules"
            rows={rules}
            paging={rulesResult.paging}
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
