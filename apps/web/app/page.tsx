import Link from "next/link";
import { RunButton } from "@/components/act";
import { MemoryMap } from "@/components/MemoryMap";
import { Term } from "@/components/Term";
import { Chip, EmptyState, JobChip, Ledger, Meter, Panel, Verdict } from "@/components/ui";
import { startReindex } from "@/lib/actions/jobs";
import { getAgentRuns, getCodeModules, getCondition, getCorpus, getEvents, getJobs, getRepository } from "@/lib/data";

type Props = { searchParams: Promise<{ repo?: string }> };

const jobWords: Record<string, string> = {
  "index.incremental": "Reading recent changes",
  "embeddings.backfill": "Making new code searchable",
  "context_pack.build": "Preparing an agent briefing",
  "graph.sync": "Updating the code map",
  "eval.golden_tasks": "Checking answer quality",
};

export default async function Overview({ searchParams }: Props) {
  const { repo: slug } = await searchParams;
  const [repo, condition, corpus, modules, jobs, events, runs] = await Promise.all([
    getRepository(slug),
    getCondition(slug),
    getCorpus(slug),
    getCodeModules(slug),
    getJobs(slug),
    getEvents(50, slug),
    getAgentRuns(slug),
  ]);
  const scanning = (repo.behind ?? 0) > 0 && jobs.some((j) => j.status === "running" && j.kind.startsWith("index"));
  const withRepo = (href: string) => (slug ? `${href}?repo=${slug}` : href);

  return (
    <>
      <Verdict
        condition={condition}
        action={
          condition.action ? (
            <RunButton action={startReindex.bind(null, { repoPath: repo.path })} label={condition.action.label} icon="refresh" variant="primary" title="Re-index" />
          ) : undefined
        }
      />

      {modules.some((m) => m.chunks > 0 && m.current + m.outdated + m.missing + m.excluded > 0) ? (
        <MemoryMap modules={modules} repoName={repo.name} scanning={scanning} />
      ) : (
        <Panel id="map" title="Memory map">
          <EmptyState
            title="No memory map yet"
            body="Brain hasn’t reported which parts of the code it has read. The map appears once a repository is indexed."
          />
        </Panel>
      )}

      <div className="grid-2">
        <div className="stack">
          <Panel
            id="meters"
            title="Can agents find everything?"
            desc={
              <>
                Brain splits code into <Term k="chunk">pieces</Term> and makes each one{" "}
                <Term k="searchable">searchable</Term>. Every brick is 5% of the code.
              </>
            }
          >
            {corpus.meters.length ? (
              <div className="meters">
                {corpus.meters.map((m) => (
                  <Meter key={m.label} meter={m} />
                ))}
              </div>
            ) : (
              <EmptyState title="No coverage numbers yet" body="Brain hasn’t reported how much of the code is searchable." />
            )}
          </Panel>

          <Panel id="queue" title="What Brain is doing right now" desc="Background work, newest first." flush>
            {jobs.length === 0 && <EmptyState title="Nothing running" body="No background work has been reported recently." />}
            <ul>
              {jobs.map((j, i) => (
                <li key={j.id} className="ledger__row job-row" style={{ "--i": i } as React.CSSProperties}>
                  <span className="ledger__time num">{j.startedAt.slice(0, 5)}</span>
                  <span className="job-row__main">
                    <span>{jobWords[j.kind] ?? j.kind}</span>
                    <span className="sub">
                      {j.repo}
                      {j.detail ? ` · ${j.detail}` : ""}
                      {j.duration ? ` · took ${j.duration}` : ""}
                    </span>
                  </span>
                  <JobChip status={j.status} />
                </li>
              ))}
            </ul>
          </Panel>
        </div>

        <div className="stack">
          <Panel
            id="activity"
            title="Latest activity"
            actions={
              <Link className="link" href={withRepo("/logs")}>
                See all
              </Link>
            }
            flush
          >
            {events.length ? (
              <Ledger events={events} limit={7} compact />
            ) : (
              <EmptyState title="No activity yet" body="Nothing has been recorded in Brain’s activity log." />
            )}
          </Panel>

          <Panel id="runs" title="Recent agent work" desc={<>Did the <Term k="agent">agent</Term> get a briefing from Brain first?</>} flush>
            {runs.length === 0 && <EmptyState title="No agent work yet" body="No agent has run a task through Brain recently." />}
            <ul>
              {runs.slice(0, 4).map((r, i) => (
                <li key={r.id} className="ledger__row job-row" style={{ "--i": i } as React.CSSProperties}>
                  <span className="ledger__time num">{r.startedAt}</span>
                  <span className="job-row__main">
                    <span>{r.task}</span>
                    <span className="sub">{r.agent}</span>
                  </span>
                  {r.packHit === undefined ? (
                    <Chip tone="idle">not tracked</Chip>
                  ) : r.packHit ? (
                    <Chip tone="ok">briefed</Chip>
                  ) : (
                    <Chip tone="warn">no briefing</Chip>
                  )}
                </li>
              ))}
            </ul>
          </Panel>
        </div>
      </div>
    </>
  );
}
