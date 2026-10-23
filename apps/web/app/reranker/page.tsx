import { adminOnlyPage } from "@/components/AdminOnly";
import { Term } from "@/components/Term";
import { SectionTabs } from "@/components/SectionNav";
import { Chip, EmptyState, Meter, PageHead, Panel, Stat, fmt } from "@/components/ui";
import { getReranker } from "@/lib/data";

export const metadata = { title: "Reranker" };

export default async function Reranker({ searchParams }: { searchParams: Promise<{ repo?: string }> }) {
  const gate = await adminOnlyPage("Reranker");
  if (gate) return gate;
  const { repo } = await searchParams;
  const r = await getReranker(repo);
  const measured = r.queries24h > 0;
  const off = r.stateText === "Turned off";
  const later = off ? "shown once it is on" : undefined;
  return (
    <>
      <SectionTabs section="settings" active="/reranker" />
      <PageHead
        title="Reranker"
        status={off && !r.notSetUp ? { tone: "warn", text: <>The second search pass is turned off<small>Search still works; results are ranked by the first pass only.</small></> } : undefined}
        lede={
          <>
            After a quick first search, a second model — the <Term k="reranker">reranker</Term> — re-checks the top results and
            moves the most relevant code to the top. This screen shows whether it is running, how fast it is and how much code it covers.
          </>
        }
        actions={
          <>
            {r.service && <Chip tone={r.service.tone}>{r.service.text}</Chip>}
            <Chip tone={r.state}>{r.stateText}</Chip>
          </>
        }
      />
      {r.notSetUp ? (
        <Panel id="optional" title="Optional, and not set up on this server">
          <p className="text-dim">
            Brain’s search works fully without it: agents get results from the first pass. The reranker is an advanced extra that
            runs a separate late-interaction model service and only re-orders results once it is set up, encoded and approved.
          </p>
          <p className="text-dim reasons__note">
            Turn it on with <span className="mono">LATE_INTERACTION_ENABLED</span> and a model service URL in the server settings.
          </p>
        </Panel>
      ) : r.available === false ? (
        <EmptyState
          title="Reranker status unavailable"
          body={
            r.unavailableReason ??
            "Brain didn’t answer the reranker status request for this project just now. Reload in a moment; if no project is indexed yet, index one first."
          }
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
