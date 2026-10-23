import { Term } from "@/components/Term";
import { SectionTabs } from "@/components/SectionNav";
import { Chip, EmptyState, JobChip, PageHead, Panel, Stat, Table, fmt } from "@/components/ui";
import { RunButton } from "@/components/act";
import { startReindex } from "@/lib/actions/jobs";
import { getCorpus, getPagedIndexRuns, getRepositoryFeed } from "@/lib/data";
import { canReindex } from "@/lib/repository-status";
import { restrictedMember } from "@/lib/team-session";
import { getListQuery } from "@/lib/list-query";
import type { IndexRun } from "@/lib/types";

export const metadata = { title: "Index runs" };

const triggerWords: Record<IndexRun["trigger"], string> = {
  push: "after a new commit",
  schedule: "nightly refresh",
  manual: "started by hand",
  "auto-heal": "automatic repair",
  unknown: "",
};

export default async function Indexing({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const repo = typeof params.repo === "string" ? params.repo : undefined;
  const hasQuery = Boolean(params.q || params.status || params.page || params.page_size);
  const [result, corpus, feed, member] = await Promise.all([getPagedIndexRuns(repo, getListQuery(params)), getCorpus(repo), getRepositoryFeed(), restrictedMember()]);
  // The selected project, or the only one there is: the run this page's lede asks for.
  const target = repo ? feed.repositories.find((r) => r.slug === repo || r.name.toLowerCase() === repo.toLowerCase()) : feed.repositories.length === 1 ? feed.repositories[0] : undefined;
  const reindex = !member && target && canReindex(target)
    ? <RunButton variant="primary" action={async () => { "use server"; return startReindex({ repoPath: target.path }); }} label={`Re-index ${target.name}`} title={`Re-index ${target.name}`} />
    : undefined;
  const runs = result.items;
  const last = runs.find((r) => r.status === "completed");
  // Facets count every run, not just this page; fall back to the page only
  // when an older API doesn't send them.
  const facets = result.paging.facets;
  const hasFacets = Object.keys(facets).length > 0;
  const troubled = hasFacets
    ? Object.entries(facets).filter(([k]) => /fail|error|degrad|time/i.test(k)).reduce((n, [, v]) => n + v, 0)
    : runs.filter((r) => r.status === "failed" || r.status === "degraded").length;
  return (
    <>
      <SectionTabs section="projects" active="/indexing" />
      <PageHead
        title="Index runs"
        actions={reindex}
        lede={
          <>
            Each time Brain read your code. Its <Term k="index">index</Term> is its own copy of committed source, cut into searchable pieces.
            Reindex after source changes; later runs re-read what changed. Scheduled or event-triggered runs depend on your configuration.
          </>
        }
      />
      <div className="stats">
        <Stat label="Files in the last good run" value={last ? fmt(last.files) : "—"} note={result.available === false ? "index history unavailable" : last ? `${last.startedAt} · took ${last.duration}` : "no completed run in this list"} />
        <Stat label="Pieces stored" value={corpus.available === false ? "—" : fmt(corpus.chunks)} note={corpus.available === false ? "indexing totals unavailable" : "searchable pieces of code Brain holds"} />
        <div className={result.available === false || troubled ? "tone-warn" : "tone-ok"}>
        <Stat label={<>Runs that failed or ran <Term k="degraded">degraded</Term></>} value={result.available === false ? "—" : String(troubled)} note={result.available === false ? "index history unavailable" : `${hasFacets ? "of" : "on this page, of"} ${result.paging.total.toLocaleString("en-US")} runs`} />
        </div>
      </div>
      <Panel id="runs" title="Index runs" desc="Newest first. “Partial” means only changed files were re-read, which is normal." flush>
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
                  {triggerWords[r.trigger] && <span className="sub">{triggerWords[r.trigger]}</span>}
                </>
              ),
            },
            {
              head: "Code version",
              cell: (r) => (
                <>
                  <span className="mono text-dim">{r.revision}</span>
                  {r.uncommitted ? (
                    <small className="text-faint" title="Brain indexed the last commit; commit these edits to include them.">
                      {" "}· {r.uncommitted} uncommitted {r.uncommitted === 1 ? "change" : "changes"} not indexed
                    </small>
                  ) : null}
                </>
              ),
            },
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
