import type { CSSProperties } from "react";
import Link from "next/link";
import { SectionTabs } from "@/components/SectionNav";
import { ClampText } from "@/components/ClampText";
import { ListFrame } from "@/components/ListFrame";
import { DecisionActions, RecordHint, RuleActions } from "@/components/MemoryActions";
import { PageTabs, pickTab } from "@/components/PageTabs";
import { Chip, EmptyState, PageHead, Panel, Table } from "@/components/ui";
import { getCapabilities } from "@/lib/capabilities";
import { getPagedDecisions, getPagedRules, getRepositoryFeed } from "@/lib/data";
import { getListQuery } from "@/lib/list-query";
import { getReviewView } from "@/lib/review-view";
import { signInMode } from "@/lib/sign-in-view";
import { currentMember } from "@/lib/team-session";
import type { Decision, Rule, Tone } from "@/lib/types";

export const metadata = { title: "Memory" };

const statusTone: Record<Decision["status"], Tone> = { accepted: "ok", proposed: "info", superseded: "idle", validated_unmerged: "info" };
const statusWords: Record<Decision["status"], string> = { accepted: "agreed", proposed: "proposed", superseded: "retired or replaced", validated_unmerged: "verified, not merged yet" };
const severity: Record<Rule["severity"], [Tone, string]> = {
  block: ["bad", "blocks the change"],
  warn: ["warn", "warns"],
  advise: ["idle", "suggests"],
};

const VIEWS = ["decisions", "rules"] as const;

/** Proposals the viewer could review, counted the way /review counts them; 0 outside a team. */
async function waitingForReview(): Promise<number> {
  const [features, mode] = await Promise.all([getCapabilities(), signInMode()]);
  if (mode !== "team" || !features.supported.includes("knowledge_review")) return 0;
  const member = await currentMember();
  const view = await getReviewView(Boolean(member?.admin));
  if (view.kind !== "ok") return 0;
  const selfId = member?.id ?? null;
  return view.queue.filter((r) => r.authorId === null || r.authorId !== selfId).length;
}

export default async function Memory({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const view = typeof params.view === "string" ? params.view : undefined;
  const tab = pickTab(VIEWS, view);
  const query = getListQuery(params);
  const hasQuery = Boolean(params.q || params.status || params.page || params.page_size);
  const repo = typeof params.repo === "string" ? params.repo : undefined;
  const [decisionsResult, rulesResult, waiting, feed] = await Promise.all([
    getPagedDecisions(repo, tab === "decisions" ? query : {}),
    getPagedRules(repo, tab === "rules" ? query : {}),
    waitingForReview(),
    getRepositoryFeed(),
  ]);
  const repoPath = repo ? feed.repositories.find((r) => r.slug === repo)?.path : undefined;
  const decisions = decisionsResult.items;
  const rules = rulesResult.items;
  // Usage is reported per page of rules, and only once something has been
  // recorded. Before that the columns are hidden rather than filled with
  // zeros: "0 delivered" and "never counted" are not the same claim.
  const usageTracked = rules.some((r) => r.delivered30d !== undefined);
  const unusedCount = rules.filter((r) => r.delivered30d === 0).length;
  const blocking = rules.filter((r) => r.severity === "block").length;
  return (
    <>
      <SectionTabs section="memory" active="/memory" />
      <PageHead
        title="Memory"
        lede={`Decisions and rules you and your agents recorded${repo ? " for this project" : " across all projects"}. Agents get the relevant ones with their context.`}
      />
      {waiting > 0 && (
        <p className="callout tone-info review-waiting" role="status">
          <span>
            <b>
              {waiting.toLocaleString("en")} proposal{waiting === 1 ? "" : "s"} waiting for review.
            </b>{" "}
            Agents don’t receive them until a Reviewer approves.
          </span>
          <Link className="btn btn--neutral" href="/review">
            Open Review
          </Link>
        </p>
      )}
        <PageTabs
        label="Decisions or rules"
        path="/memory"
          current={tab}
          params={{ repo }}
        tabs={[
          { id: "decisions", label: "Decisions", badge: decisionsResult.available === false ? "—" : decisionsResult.paging.total },
          { id: "rules", label: "Rules", badge: rulesResult.available === false ? "—" : rulesResult.paging.total },
        ]}
      />
      {tab === "decisions" ? (
        <section aria-label="Decisions">
          {decisionsResult.available === false ? (
            <EmptyState title="Decisions unavailable" body="Brain didn’t return the recorded decisions just now. Reload in a moment." />
          ) : decisions.length === 0 && !hasQuery ? (
            <EmptyState title="No decisions recorded" body="Nobody has recorded a decision yet. Agents and people can record one through Brain." action={<RecordHint kind="decision" />} />
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
              <ClampText text={d.summary} label="decision" />
              <p className="card__meta">
                <span className="mono">{d.scope}</span>
                <span>recorded {d.recordedAt}</span>
              </p>
              <DecisionActions decision={d} />
            </article>
          ))}
          />
          )}
          {decisions.length > 0 && <MoreHint kind="decision" />}
        </section>
      ) : (
        <Panel id="rules" title="Rules agents must follow" desc={rulesResult.available === false ? undefined : `${blocking} blocking rule${blocking === 1 ? "" : "s"} on this page. ${usageTracked ? `Last 30 days: “Reached” counts the context packs a rule went into, “Caught” the reviews that cited it${unusedCount ? `; ${unusedCount} reached no agent at all` : ""}.` : "Nothing is recorded yet, so this page can’t say which rules reach agents."}`}
          flush
        >
          {rulesResult.available === false ? (
            <EmptyState title="Rules unavailable" body="Brain didn’t return the recorded rules just now. Reload in a moment." />
          ) : rules.length === 0 && !hasQuery ? (
            <EmptyState title="No rules yet" body="No active rules are recorded. Add one so agents’ changes are checked against it." action={<RecordHint kind="rule" repoPath={repoPath} />} />
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
              // A rule that reached nobody is the actionable case, so zero is
              // shown as such and never as a dash — the dash is reserved for
              // "not counted", which is a different problem.
              ...(usageTracked
                ? [
                    {
                      head: "Reached",
                      cell: (r: Rule) =>
                        r.delivered30d === undefined ? (
                          "—"
                        ) : r.delivered30d === 0 ? (
                          <Chip tone="warn">never</Chip>
                        ) : (
                          r.delivered30d.toLocaleString("en")
                        ),
                      align: "right" as const,
                    },
                    {
                      head: "Caught",
                      cell: (r: Rule) => (r.caught30d === undefined ? "—" : r.caught30d.toLocaleString("en")),
                      align: "right" as const,
                    },
                  ]
                : []),
              { head: <span className="sr-only">Actions</span>, cell: (r: Rule) => <RuleActions rule={r} />, align: "right" as const },
            ]}
          />
          )}
          {rules.length > 0 && <MoreHint kind="rule" repoPath={repoPath} />}
        </Panel>
      )}
    </>
  );
}

/** Below a filled list: where new entries come from, folded away. */
function MoreHint({ kind, repoPath }: { kind: "decision" | "rule"; repoPath?: string }) {
  return (
    <details className="record-more">
      <summary>Record a new {kind}</summary>
      <RecordHint kind={kind} repoPath={repoPath} />
    </details>
  );
}
