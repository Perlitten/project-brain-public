import { Term } from "@/components/Term";
import { Chip, EmptyState, JobChip, PageHead, Panel, Stat, Table, fmt } from "@/components/ui";
import { getAgentRuns } from "@/lib/data";

export const metadata = { title: "Agent runs" };

export default async function Runs() {
  const runs = await getAgentRuns();
  const briefed = runs.filter((r) => r.packHit).length;
  const briefingTracked = runs.some((r) => r.packHit !== undefined);
  const failed = runs.filter((r) => r.status === "failed" || r.status === "error").length;
  // Tasks nobody closed get closed by the reaper after their deadline. That's
  // not a failure: the agent may well have finished and just not reported it.
  const timedOut = runs.filter((r) => r.status === "timed_out").length;
  return (
    <>
      <PageHead
        eyebrow="Agent runs"
        title="What AI agents did with Brain’s help"
        lede={
          <>
            Every task an <Term k="agent">agent</Term> ran while connected to Brain. Agents that got a briefing first usually
            finish faster and fail less.
          </>
        }
      />
      <div className="stats">
        <Stat label="Tasks" value={String(runs.length)} note="recently" />
        <div className="tone-ok">
          <Stat
            label="Got a briefing"
            value={briefingTracked ? `${briefed}/${runs.length}` : "—"}
            note={briefingTracked ? "started with a context pack" : "not tracked yet"}
          />
        </div>
        <div className={failed ? "tone-bad" : "tone-ok"}>
          <Stat label="Failed" value={String(failed)} note="ended with an error" />
        </div>
        {timedOut > 0 && (
          <div className="tone-warn">
            <Stat label="Timed out" value={String(timedOut)} note="never reported back; closed after the deadline" />
          </div>
        )}
      </div>
      <Panel id="runs" title="Recent tasks" flush>
        {runs.length === 0 ? (
          <EmptyState title="No agent tasks yet" body="No agent has run a task through Brain recently." />
        ) : (
        <Table
          caption="Agent runs"
          rows={runs}
          rowKey={(r) => r.id}
          filter={{ search: (r) => `${r.task} ${r.id} ${r.agent}`, facet: { label: "Result", of: (r) => r.status, values: [{ value: "timed_out", label: "timed out" }] }, noun: ["task", "tasks"] }}
          columns={[
            {
              head: "Task",
              cell: (r) => (
                <>
                  <span>{r.task}</span>
                  <span className="sub mono">
                    {r.id} · {r.agent}
                  </span>
                </>
              ),
            },
            {
              head: "Briefing",
              cell: (r) =>
                r.packHit === undefined ? <Chip tone="idle">not tracked</Chip> : r.packHit ? <Chip tone="ok">yes</Chip> : <Chip tone="warn">none</Chip>,
            },
            { head: <Term k="tokens">Tokens</Term>, cell: (r) => (r.tokens === undefined ? "—" : fmt(r.tokens)), align: "right" },
            { head: "Took", cell: (r) => r.duration, align: "right" },
            { head: "Result", cell: (r) => <JobChip status={r.status} /> },
          ]}
        />
        )}
      </Panel>
    </>
  );
}
