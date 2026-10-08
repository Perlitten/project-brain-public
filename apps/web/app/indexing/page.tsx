import { Term } from "@/components/Term";
import { SectionNav } from "@/components/SectionNav";
import { Chip, EmptyState, JobChip, PageHead, Panel, Stat, Table, fmt } from "@/components/ui";
import { getCorpus, getPagedIndexRuns } from "@/lib/data";
import { getListQuery } from "@/lib/list-query";
import type { IndexRun } from "@/lib/types";

export const metadata = { title: "Indexing" };

const triggerWords: Record<IndexRun["trigger"], string> = {
  push: "after a new commit",
  schedule: "nightly refresh",
  manual: "started by hand",
  "auto-heal": "automatic repair",
  unknown: "trigger not recorded",
};

export default async function Indexing({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const repo = typeof params.repo === "string" ? params.repo : undefined;
  const hasQuery = Boolean(params.q || params.status || params.page || params.page_size);
  const [result, corpus] = await Promise.all([getPagedIndexRuns(repo, getListQuery(params)), getCorpus(repo)]);
  const runs = result.items;
  const last = runs.find((r) => r.status === "completed");
  const troubled = runs.filter((r) => r.status === "failed" || r.status === "degraded").length;
  return (
    <>
      <PageHead
        eyebrow="Indexing"
        title="Every time Brain re-read your code"
        lede={
          <>
            Brain keeps an <Term k="index">index</Term> — its own copy of your code, cut into searchable pieces. It refreshes
            after every commit and once a night.
          </>
        }
      />
      <SectionNav label="Project views" items={[{ href: "/projects", label: "Projects" }, { href: "/indexing", label: "Indexing", active: true }, { href: "/graph", label: "Code map" }, { href: "/setup", label: "Connection" }]} />
      <div className="stats">
        <Stat label="Files in last completed read on this page" value={last ? fmt(last.files) : "—"} note={result.available === false ? "index history unavailable" : last ? `took ${last.duration}` : "no completed run on this page"} />
        <Stat label="Pieces stored" value={corpus.available === false ? "—" : fmt(corpus.chunks)} note={corpus.available === false ? "indexing totals unavailable" : "searchable slices across the repository"} />
        <div className={result.available === false || troubled ? "tone-warn" : "tone-ok"}>
        <Stat label="Runs with problems" value={result.available === false ? "—" : String(troubled)} note={result.available === false ? "index history unavailable" : `on this page · ${result.paging.total.toLocaleString("en-US")} total`} />
        </div>
      </div>
      <Panel id="runs" title="Index runs" desc="A partial run only re-reads the files that changed." flush>
        {result.available === false ? (
          <EmptyState title="Index history unavailable" body="Brain did not return scoped index history. Try again when the indexing service is reachable." />
        ) : runs.length === 0 && !hasQuery ? (
          <EmptyState title="No index runs yet" body="Brain hasn’t reported any runs. Index a repository to see them here." />
        ) : (
        <Table
          caption="Index runs"
          rows={runs}
          paging={result.paging}
          rowKey={(r) => String(r.id)}
          filter={{ search: (r) => `#${r.id} ${r.revision} ${r.startedAt} ${r.trigger}`, facet: { label: "Result", of: (r) => r.status }, noun: ["run", "runs"] }}
          columns={[
            { head: "Run", cell: (r) => <span className="num">#{r.id}</span> },
            {
              head: "When",
              cell: (r) => (
                <>
                  <span>{r.startedAt}</span>
                  <span className="sub">{triggerWords[r.trigger]}</span>
                </>
              ),
            },
            { head: "Code version", cell: (r) => <span className="mono text-dim">{r.revision}</span> },
            { head: "Files changed", cell: (r) => fmt(r.changed), align: "right" },
            { head: "Took", cell: (r) => r.duration, align: "right" },
            {
              head: "Result",
              cell: (r) => (
                <span className="chips">
                  <JobChip status={r.status} />
                  {r.completeness === "partial" && <Chip tone="idle" dot={false}>partial</Chip>}
                  {r.completeness === "aborted" && <Chip tone="bad" dot={false}>stopped early</Chip>}
                </span>
              ),
            },
          ]}
        />
        )}
      </Panel>
    </>
  );
}
