import { Term } from "@/components/Term";
import { Chip, EmptyState, Meter, PageHead, Panel, Stat, fmt } from "@/components/ui";
import { getReranker } from "@/lib/data";

export const metadata = { title: "Search quality" };

export default async function Reranker() {
  const r = await getReranker();
  const measured = r.queries24h > 0;
  return (
    <>
      <PageHead
        eyebrow="Search quality"
        title="How well Brain puts the right code first"
        lede={
          <>
            After a quick first search, a second model — the <Term k="reranker">reranker</Term> — re-checks the top results and
            moves the most relevant code to the top. This screen shows whether it is running and how much it helps.
          </>
        }
        actions={<Chip tone={r.state}>{r.stateText}</Chip>}
      />
      {r.available === false ? (
        <EmptyState
          title="No reranker status"
          body="Brain didn’t report the state of its reranker. It may be turned off, or no repository is indexed yet."
        />
      ) : (
      <>
      <div className="stats">
        <div className={r.ndcgLift === "—" ? undefined : "tone-ok"}>
          <Stat label="Ranking gain" value={r.ndcgLift} note="better ordering on our test questions" />
        </div>
        <Stat label="Typical search" value={measured ? `${r.p50ms}ms` : "—"} note="half of searches are faster" />
        <Stat label="Slowest searches" value={measured ? `${r.p95ms}ms` : "—"} note="1 in 20 takes longer than this" />
        <Stat label="Searches" value={fmt(r.queries24h)} note={r.queriesWindow ?? "in the last 24 hours"} />
      </div>
      <Panel
        id="coverage"
        title="How much code is ready for the second check"
        desc={
          <>
            Model <span className="mono">{r.model}</span>. Pieces not ready yet still get the basic search. Slow searches are
            measured as <Term k="p95">p95</Term>.
          </>
        }
      >
        {r.coverage.parts.length ? (
          <Meter meter={r.coverage} />
        ) : (
          <EmptyState title="Nothing encoded yet" body="No code has been prepared for the second check yet." />
        )}
      </Panel>
      </>
      )}
    </>
  );
}
