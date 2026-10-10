import { EmptyState, Ledger, PageHead, Panel } from "@/components/ui";
import { SectionNav } from "@/components/SectionNav";
import { BackgroundJobs } from "@/components/BackgroundJobs";
import { getJobs, getPagedEvents } from "@/lib/data";
import { getListQuery } from "@/lib/list-query";
import Link from "next/link";
import { RequestHistory } from "@/components/RequestHistory";
import { dashboardPeriod, getRequestTelemetry } from "@/lib/dashboard";
import { getRepositories } from "@/lib/data";

export const metadata = { title: "Activity" };

export default async function Activity({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const repo = typeof params.repo === "string" ? params.repo : undefined;
  const period = dashboardPeriod(typeof params.period === "string" ? params.period : undefined);
  const repositories = await getRepositories();
  const selected = repo ? repositories.find((r) => r.slug === repo) : undefined;
  const projects = repo ? selected ? [selected] : [] : repositories;
  const [result, jobLists, telemetry] = await Promise.all([getPagedEvents(repo, getListQuery(params)), Promise.all(projects.map((p) => getJobs(p.slug))), getRequestTelemetry(period, repo ? selected?.id ?? 0 : undefined)]);
  const jobs = [...new Map(jobLists.flatMap((list) => list.slice(0, 5)).map((j) => [j.id, j])).values()];
  const events = result.items;
  const hasQuery = Boolean(params.q || params.status || params.page || params.page_size);
  return <>
    <PageHead title="Activity" lede="What agents asked Brain and what Brain ran in the background, newest first." />
    <SectionNav label="Activity views" items={[{ href: "/logs", label: "Activity", active: true }, { href: "/runs", label: "Agent runs" }]} />
    <Panel id="requests" title="Agent requests" desc="The latest search and context calls, retries included. Handling time is measured on the server only." actions={<nav className="segmented" aria-label="Request period">{(["24h", "7d", "30d"] as const).map((p) => <Link key={p} scroll={false} aria-current={p === period ? "page" : undefined} href={`/logs?${new URLSearchParams({ ...(repo ? { repo } : {}), period: p })}`}>{p}</Link>)}</nav>} flush>
      <RequestHistory telemetry={telemetry} />
    </Panel>
    <Panel id="jobs" title="Background jobs" desc="Indexing and checks Brain ran recently. A failed job shows its recorded error." flush>
      <BackgroundJobs jobs={jobs} />
    </Panel>
    <Panel id="events" title="Event log" desc="Other events Brain recorded, newest first." flush>
      {events.length || hasQuery ? <Ledger events={events} paging={result.paging} /> : <EmptyState title="No recorded events" body="The event log is empty. Background job history is shown above." />}
    </Panel>
  </>;
}
