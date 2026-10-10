import Link from "next/link";
import { SectionTabs } from "@/components/SectionNav";
import { Term } from "@/components/Term";
import { Chip, EmptyState, PageHead, Panel, Stat, Table, fmt } from "@/components/ui";
import { getPagedContextPacks } from "@/lib/data";
import { getListQuery, listQueryParams } from "@/lib/list-query";

export const metadata = { title: "Context packs" };

export default async function Packs({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const query = getListQuery(params);
  const repo = typeof params.repo === "string" ? params.repo : undefined;
  const result = await getPagedContextPacks(query, repo);
  const listParams = new URLSearchParams(listQueryParams(query));
  if (repo) listParams.set("repo", repo);
  const back = `/packs${listParams.size ? `?${listParams}` : ""}`;
  const hasQuery = Boolean(params.q || params.status || params.page || params.page_size);
  const packs = result.items;
  const stale = result.paging.facets.outdated ?? 0;
  const unknown = result.paging.facets.unknown ?? 0;
  const state = (p: (typeof packs)[number]) => (p.stale === null ? "unknown" : p.stale ? "outdated" : "fresh");
  const stateWords = { fresh: "current", outdated: "older code", unknown: "revision unknown" };
  const avg = packs.length ? Math.round(packs.reduce((s, p) => s + p.tokens, 0) / packs.length) : 0;
  return (
    <>
      <PageHead
        title="Context packs"
        lede={
          <>
            Before an agent starts a task, Brain hands it a <Term k="pack">context pack</Term>: only the files, decisions and
            rules that matter. {repo ? "Packs built for this project." : "Packs built across all projects."}
          </>
        }
      />
      <SectionTabs section="memory" active="/packs" />
      <div className="stats">
        <Stat label="Context packs" value={fmt(result.paging.total)} note={hasQuery ? "matching the filters" : repo ? "for this project" : "across all projects"} />
        <Stat label="Average size on this page" value={packs.length ? fmt(avg) : "—"} note="tokens, about ¾ of a word each" />
        {unknown > 0 && stale === 0 && !result.paging.facets.fresh ? (
          <Stat label="Built for older code" value="—" note="unknown: these packs predate revision tracking" />
        ) : (
          <div className={stale ? "tone-warn" : "tone-ok"}>
            <Stat label="Built for older code" value={fmt(stale)} note={unknown ? `${fmt(unknown)} more predate revision tracking` : "the code has changed since they were built"} />
          </div>
        )}
      </div>
      <Panel id="packs" title="Recent context packs" desc="Older code: built before the latest index run, so file contents may have changed since. Revision unknown: built before Brain recorded revisions. Both are history, not errors." flush>
        {packs.length === 0 && !hasQuery ? (
          <EmptyState title="No context packs yet" body="Brain hasn’t prepared a context pack for any agent recently. Try one from Setup, step 6." action={<Link className="btn btn--neutral" href="/setup#first_task">Build one</Link>} />
        ) : (
        <Table
          caption="Context packs"
          rows={packs}
          paging={result.paging}
          rowKey={(p) => p.id}
          filter={{
            search: (p) => `${p.task} ${p.id} ${p.consumer}`,
            facet: { label: "State", of: state, values: [{ value: "outdated", label: "older code" }, { value: "fresh", label: "current" }, { value: "unknown", label: "revision unknown" }] },
            noun: ["pack", "packs"],
          }}
          columns={[
            {
              head: "Task",
              cell: (p) => (
                <>
                  <Link className="link" href={`/packs/${encodeURIComponent(p.id)}?${repo ? `repo=${encodeURIComponent(repo)}&` : ""}back=${encodeURIComponent(back)}`}>{p.task}</Link>
                  <span className="sub mono">
                    #{p.id}
                    {p.consumer !== "an agent" ? ` · for ${p.consumer}` : ""}
                  </span>
                </>
              ),
            },
            { head: "Files", cell: (p) => p.files, align: "right" },
            { head: <Term k="tokens">Tokens</Term>, cell: (p) => fmt(p.tokens), align: "right" },
            { head: "Built", cell: (p) => p.createdAt },
            { head: "State", cell: (p) => <Chip tone={p.stale === null ? "idle" : p.stale ? "warn" : "ok"}>{stateWords[state(p)]}</Chip> },
          ]}
        />
        )}
      </Panel>
    </>
  );
}
