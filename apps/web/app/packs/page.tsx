import Link from "next/link";
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
  const avg = packs.length ? Math.round(packs.reduce((s, p) => s + p.tokens, 0) / packs.length) : 0;
  return (
    <>
      <PageHead
        eyebrow="Context packs"
        title="Briefings Brain prepared for AI agents"
        lede={
          <>
            Before an agent starts a task, Brain hands it a <Term k="pack">context pack</Term>: only the files, decisions and
            rules that matter. {repo ? "Briefings for the selected repository." : "Briefings across all repositories."}
          </>
        }
      />
      <div className="stats">
        <Stat label="Briefings" value={String(result.paging.total)} note="matching briefings" />
        <Stat label="Average size on this page" value={packs.length ? fmt(avg) : "—"} note="tokens, about ¾ of a word each" />
        {unknown > 0 && stale === 0 && !result.paging.facets.fresh ? (
          <Stat label="Outdated" value="—" note="unknown: these packs don’t record the commit they were built from" />
        ) : (
          <div className={stale ? "tone-warn" : "tone-ok"}>
            <Stat label="Outdated" value={String(stale)} note={unknown ? `matching search · ${unknown} unknown` : "matching search, before the state filter"} />
          </div>
        )}
      </div>
      <Panel id="packs" title="Recent briefings" flush>
        {packs.length === 0 && !hasQuery ? (
          <EmptyState title="No briefings yet" body="Brain hasn’t prepared a context pack for any agent recently." />
        ) : (
        <Table
          caption="Context packs"
          rows={packs}
          paging={result.paging}
          rowKey={(p) => p.id}
          filter={{
            search: (p) => `${p.task} ${p.id} ${p.consumer}`,
            facet: { label: "State", of: state, values: [{ value: "outdated" }, { value: "fresh" }, { value: "unknown" }] },
            noun: ["pack", "packs"],
          }}
          columns={[
            {
              head: "Task",
              cell: (p) => (
                <>
                  <Link className="link" href={`/packs/${encodeURIComponent(p.id)}?back=${encodeURIComponent(back)}`}>{p.task}</Link>
                  <span className="sub mono">
                    {p.id} · for {p.consumer}
                  </span>
                </>
              ),
            },
            { head: "Files", cell: (p) => p.files, align: "right" },
            { head: <Term k="tokens">Tokens</Term>, cell: (p) => fmt(p.tokens), align: "right" },
            { head: "Built", cell: (p) => p.createdAt },
            { head: "State", cell: (p) => <Chip tone={p.stale === null ? "idle" : p.stale ? "warn" : "ok"}>{state(p)}</Chip> },
          ]}
        />
        )}
      </Panel>
    </>
  );
}
