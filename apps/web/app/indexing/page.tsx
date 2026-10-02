import { Term } from "@/components/Term";
import { Chip, EmptyState, JobChip, PageHead, Panel, Stat, Table, fmt } from "@/components/ui";
import { getIndexRuns } from "@/lib/data";
import type { IndexRun } from "@/lib/types";

export const metadata = { title: "Indexing" };

const triggerWords: Record<IndexRun["trigger"], string> = {
  push: "after a new commit",
  schedule: "nightly refresh",
  manual: "started by hand",
  "auto-heal": "automatic repair",
  unknown: "trigger not recorded",
};

export default async function Indexing() {
  const runs = await getIndexRuns();
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
      <div className="stats">
        <Stat label="Files in the last read" value={last ? fmt(last.files) : "0"} note={last ? `took ${last.duration}` : "no complete read yet"} />
        <Stat label="Pieces stored" value={last ? fmt(last.chunks) : "0"} note="searchable slices of code" />
        <div className={troubled ? "tone-warn" : "tone-ok"}>
          <Stat label="Runs with problems" value={String(troubled)} note={`out of the last ${runs.length}`} />
        </div>
      </div>
      <Panel id="runs" title="Index runs" desc="A partial run only re-reads the files that changed." flush>
        {runs.length === 0 ? (
          <EmptyState title="No index runs yet" body="Brain hasn’t reported any runs. Index a repository to see them here." />
        ) : (
        <Table
          caption="Index runs"
          rows={runs}
          rowKey={(r) => String(r.id)}
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
