import { groupFindings } from "@/lib/finding-groups";
import { FindingActions } from "@/components/FindingActions";
import { scopeMemberToProject } from "@/lib/member-scope";
import { restrictedMember } from "@/lib/team-session";
import type { CSSProperties, ReactNode } from "react";
import Link from "next/link";
import { Term } from "@/components/Term";
import { ClampText } from "@/components/ClampText";
import { ListFrame } from "@/components/ListFrame";
import { Chip, EmptyState, PageHead } from "@/components/ui";
import { moduleOf } from "@/lib/code-graph";
import { getPagedInsights } from "@/lib/data";
import { getListQuery } from "@/lib/list-query";
import type { Insight } from "@/lib/types";
import { SectionTabs } from "@/components/SectionNav";

export const metadata = { title: "Findings" };

const kindWords: Record<Insight["kind"], ReactNode> = {
  drift: <Term k="drift">Structure drift</Term>,
  coupling: <Term k="coupling">Tangled modules</Term>,
  hotspot: <Term k="hotspot">Hotspot</Term>,
  freshness: <><Term k="pack">Context pack</Term> for older code</>,
  other: "Observation",
};

export default async function Insights({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  await scopeMemberToProject("/insights", params);
  const [result, member] = await Promise.all([getPagedInsights(typeof params.repo === "string" ? params.repo : undefined, getListQuery(params)), restrictedMember()]);
  const insights = result.items;
  const hasQuery = Boolean(params.q || params.status || params.page || params.page_size);
  const repo = typeof params.repo === "string" ? params.repo : undefined;
  // A finding's module opens the code map focused on it.
  const mapLink = (name: string) => {
    const module = moduleOf(name);
    return module ? `/graph?${new URLSearchParams({ ...(repo ? { repo } : {}), module })}` : null;
  };

  const groups = groupFindings(insights);
  return (
    <>
      <SectionTabs section="projects" active="/insights" />
      <PageHead
        title="Findings"
        lede={typeof params.repo === "string" ? "Places in this project’s code that are getting harder to change." : "Places in connected repositories that are getting harder to change, across every project."}
      />
      <p className="text-faint">Duplicates are grouped on this page using evidence identity; × counts recorded occurrences. Code opens only for file evidence with an explicit repository identity.</p>
      {result.available === false && (
        <EmptyState title="Findings unavailable" body="Brain didn’t return its findings just now. Reload in a moment; nothing here means the code is clean." />
      )}
      {result.available !== false && insights.length === 0 && !hasQuery && (
        <EmptyState title="No findings yet" body="Brain hasn’t flagged anything in this code. Findings appear after a repository is indexed — run `brain index` or connect a project in Setup." action={member ? undefined : <Link className="btn btn--neutral" href="/setup">Set up indexing</Link>} />
      )}
      {result.available !== false && (insights.length > 0 || hasQuery) && (
      <ListFrame
        listTag="div"
        listClass="cards"
        noun={["finding", "findings"]}
        meta={groups.map((g) => ({ q: `${g.rep.title} ${g.rep.evidence} ${g.rep.module} ${g.rep.kind}`.toLowerCase(), f: g.rep.tone }))}
        paging={result.paging}
        facet={{ label: "Severity", values: [{ value: "bad", label: "Rule broken" }, { value: "warn", label: "Worth a look" }, { value: "info", label: "FYI" }] }}
        rows={groups.map((g, i) => (
          <article key={g.rep.id} className={`card tone-${g.rep.tone}`} style={{ "--i": Math.min(i, 12) } as CSSProperties}>
            <div className="card__top">
              <Chip tone={g.rep.tone}>{g.rep.tone === "bad" ? "Rule broken" : g.rep.tone === "warn" ? "Worth a look" : "FYI"}</Chip>
              <span className="text-dim">{kindWords[g.rep.kind]}</span>
              {g.count > 1 && <Chip tone="idle">×{g.count}</Chip>}
            </div>
            <h2 className="card__title">{g.rep.title}</h2>
            <ClampText text={g.rep.evidence} className="card__body mono" lines={6} label="evidence" />
            <p className="card__meta">
              {mapLink(g.rep.module) ? (
                <Link className="mono link" href={mapLink(g.rep.module)!} title="Show this module on the code map">{g.rep.module}</Link>
              ) : (
                <span className="mono">{g.rep.module}</span>
              )}
              <span>found {g.rep.detectedAt}{g.count > 1 ? ` · seen ${g.count} times` : ""}</span>
            </p>
            {!member && <FindingActions id={g.rep.id} title={g.rep.title} evidence={g.rep.evidence} module={g.rep.module} codeAvailable={g.rep.codeAvailable === true} targets={g.members.filter(f => f.lastSeenAt && /^\d+$/.test(f.id)).map(f => ({ id: f.id, lastSeenAt: f.lastSeenAt! }))} />}
          </article>
        ))}
      />
      )}
    </>
  );
}
