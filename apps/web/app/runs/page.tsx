import { adminOnlyPage } from "@/components/AdminOnly";
import Link from "next/link";
import { Term } from "@/components/Term";
import { SectionTabs } from "@/components/SectionNav";
import { Chip, EmptyState, PageHead, Panel, Stat, Table, fmt } from "@/components/ui";
import { getContextPacks, getPagedAgentRuns, getRepositoryFeed } from "@/lib/data";
import { packForRun } from "@/lib/task-links";
import { TaskResult } from "@/components/TaskActions";
import { NewTaskButton } from "@/components/NewTaskButton";
import { agentHref } from "@/lib/agent-link";
import { getListQuery } from "@/lib/list-query";

export const metadata = { title: "Agent tasks" };

export default async function Runs({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const gate = await adminOnlyPage("Agent tasks");
  if (gate) return gate;
  const params = await searchParams;
  const one = (k: string) => (typeof params[k] === "string" ? (params[k] as string) : undefined);
  const [result, packs, feed] = await Promise.all([getPagedAgentRuns(one("repo"), getListQuery(params)), getContextPacks(200), getRepositoryFeed()]);
  const runs = result.items;
  const projects = feed.repositories.map((r) => ({ slug: r.slug, name: r.name, path: r.path }));
  // ?new=task&title=…&goal=… opens the dialog filled in (a finding turned into work).
  const prefill = one("new") === "task" ? { title: one("title")?.slice(0, 300), goal: one("goal")?.slice(0, 10000) } : undefined;
  const back = `/runs${params.q ? `?q=${encodeURIComponent(String(params.q))}` : ""}`;
  const hasQuery = Boolean(params.q || params.status || params.page || params.page_size);
  const briefed = runs.filter((r) => r.packHit).length;
  const briefingTracked = runs.some((r) => r.packHit !== undefined);
  const failed = runs.filter((r) => r.status === "failed" || r.status === "error").length;
  // Tasks nobody closed get closed by the reaper after their deadline. That's
  // not a failure: the agent may well have finished and just not reported it.
  const timedOut = runs.filter((r) => r.status === "timed_out").length;
  return (
    <>
      <SectionTabs section="logs" active="/runs" />
      <PageHead
        title="Agent tasks"
        lede={
          <>
            Every task an <Term k="agent">agent</Term> told Brain it was starting, and whether it got a context pack for it. Agents report the end
            themselves; a task nobody closes is marked timed out, which doesn’t mean the work failed.
          </>
        }
        actions={<NewTaskButton projects={projects} prefill={prefill} />}
      />
      {result.available !== false && <div className="stats">
        <Stat label="Tasks" value={fmt(result.paging.total)} note={hasQuery ? "matching the filters" : "all recorded"} />
        <div className="tone-ok">
          <Stat
            label="Started with a context pack"
            value={briefingTracked ? `${briefed}/${runs.length}` : "—"}
            note={briefingTracked ? "on this page" : runs.length ? "this Brain version doesn’t record it" : "no tasks on this page"}
          />
        </div>
        <div className={failed ? "tone-bad" : "tone-ok"}>
          <Stat label="Failed on this page" value={String(failed)} note="ended with an error" />
        </div>
        {timedOut > 0 && (
          <div className="tone-warn">
            <Stat label="No end reported, on this page" value={String(timedOut)} note="closed automatically after the deadline" />
          </div>
        )}
      </div>}
      <Panel id="runs" title="Recent tasks" flush>
        {result.available === false ? (
          <EmptyState title="Agent tasks unavailable" body="Brain didn’t return its task history just now. Reload in a moment." />
        ) : runs.length === 0 && !hasQuery ? (
          <EmptyState title="No agent tasks yet" body="No agent has run a task through Brain recently. Connect an agent in Setup and give it a task — it will show up here." action={<Link className="btn btn--neutral" href="/setup">Connect an agent</Link>} />
        ) : (
        <Table
          caption="Agent runs"
          rows={runs}
          paging={result.paging}
          rowKey={(r) => r.id}
          filter={{ search: (r) => `${r.task} ${r.id} ${r.agent}`, facet: { label: "Result", of: (r) => r.status, values: [{ value: "timed_out", label: "no end reported" }] }, noun: ["task", "tasks"] }}
          columns={[
            {
              head: "Task",
              cell: (r) => (
                <>
                  <span>{r.task}</span>
                  <span className="sub mono" title={r.id}>
                    {agentHref(r.agent) ? <Link className="link" href={agentHref(r.agent)!}>{r.agent}</Link> : r.agent} · {r.id.length > 12 ? r.id.slice(0, 8) : r.id}
                  </span>
                </>
              ),
            },
            {
              head: "Context pack",
              cell: (r) => {
                if (r.packHit === undefined) return <span className="text-faint">—</span>;
                if (!r.packHit) return <Chip tone="warn">none</Chip>;
                const pack = packForRun(r, packs);
                return pack ? <Link className="link" href={`/packs/${encodeURIComponent(pack.id)}?back=${encodeURIComponent(back)}`}>pack #{pack.id}</Link> : <Chip tone="ok">yes</Chip>;
              },
            },
            { head: <Term k="tokens">Tokens</Term>, cell: (r) => (r.tokens === undefined ? "—" : fmt(r.tokens)), align: "right" },
            { head: "Took", cell: (r) => r.duration, align: "right" },
            { head: "Result", cell: (r) => <TaskResult run={r} /> },
          ]}
        />
        )}
      </Panel>
    </>
  );
}
