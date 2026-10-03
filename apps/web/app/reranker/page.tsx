import { Term } from "@/components/Term";
import { Chip, EmptyState, Meter, PageHead, Panel, Stat, fmt } from "@/components/ui";
import { getReranker } from "@/lib/data";

export const metadata = { title: "Search quality" };

export default async function Reranker() {
  const r = await getReranker();
  const measured = r.queries24h > 0;
  const off = r.stateText === "Turned off";
  const later = off ? "shown once it is on" : undefined;
  return (
    <>
      <PageHead
        eyebrow="Search quality"
        title={off ? "The second search pass is turned off" : "How well Brain puts the right code first"}
        lede={
          <>
            After a quick first search, a second model — the <Term k="reranker">reranker</Term> — re-checks the top results and
            moves the most relevant code to the top. This screen shows whether it is running and how much it helps.
          </>
        }
        actions={
          <>
            {r.service && <Chip tone={r.service.tone}>{r.service.text}</Chip>}
            <Chip tone={r.state}>{r.stateText}</Chip>
          </>
        }
      />
      {r.available === false ? (
        <EmptyState
          title="No reranker status"
          body="Brain didn’t report the state of its reranker. It may be turned off, or no repository is indexed yet."
        />
      ) : (
        <>
          {r.offReasons && r.offReasons.length > 0 && (
            <Panel id="why" title={off ? "Why it is off" : "What needs attention"} spine="warn">
              <p className="text-dim">
                Search still works: answers come from the first, quicker pass only. Nothing below is broken — the second pass is
                simply not allowed to touch live searches yet.
              </p>
              <ul className="reasons">
                {r.offReasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            </Panel>
          )}
          <div className="stats">
            <div className={r.ndcgLift === "—" ? undefined : "tone-ok"}>
              <Stat label="Ranking gain" value={r.ndcgLift} note={later ?? "better ordering on our test questions"} />
            </div>
            <Stat label="Typical search" value={measured ? `${r.p50ms}ms` : "—"} note={later ?? "half of searches are faster"} />
            <Stat label="Slowest searches" value={measured ? `${r.p95ms}ms` : "—"} note={later ?? "1 in 20 takes longer than this"} />
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
              <>
                <Meter meter={r.coverage} />
                {r.index && (
                  <p className="text-dim reasons__note">
                    Index <span className="mono">{r.index.revision}</span>
                    {r.index.updated && <> · built {r.index.updated}</>}
                    {r.index.approvedRevision && r.index.approvedRevision !== r.index.revision && (
                      <>
                        {" "}
                        · approved for live use: <span className="mono">{r.index.approvedRevision}</span>
                        {r.index.approvedDocuments ? ` (${fmt(r.index.approvedDocuments)} documents)` : ""}
                      </>
                    )}
                  </p>
                )}
              </>
            ) : (
              <EmptyState title="Nothing encoded yet" body="No code has been prepared for the second check yet." />
            )}
          </Panel>
        </>
      )}
    </>
  );
}
