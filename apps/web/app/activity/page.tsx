import { Suspense } from "react";
import { adminOnlyPage } from "@/components/AdminOnly";
import { EmptyState, Ledger, PageHead, Panel } from "@/components/ui";
import { SectionTabs } from "@/components/SectionNav";
import { BackgroundJobs } from "@/components/BackgroundJobs";
import { getJobs, getPagedEvents } from "@/lib/data";
import { getListQuery } from "@/lib/list-query";
import Link from "next/link";
import { RequestHistory } from "@/components/RequestHistory";
import { dashboardPeriod, getRequestTelemetry } from "@/lib/dashboard";
import { getRepositories } from "@/lib/data";
import { PageTabs, ViewPane, pickTab, type TabTone } from "@/components/PageTabs";
import { ActivityViewHash } from "@/components/ActivityViewHash";

const VIEWS = ["requests", "jobs", "events"] as const;
const JOB_FAILED = new Set(["failed", "error"]);
const JOB_RUNNING = new Set(["running", "retrying", "queued"]);

export const metadata = { title: "Activity" };

export default async function Activity({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const gate = await adminOnlyPage("Activity");
  if (gate) return gate;
  return <>
    <SectionTabs section="logs" active="/activity" />
    <PageHead title="Activity" lede="What agents asked Brain, and what Brain ran in the background on its own. Newest first." />
    <Suspense fallback={<p role="status" aria-live="polite">Loading measurements…</p>}><ActivityData searchParams={searchParams} /></Suspense>
  </>;
}

async function ActivityData({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const repo = typeof params.repo === "string" ? params.repo : undefined;
  const period = dashboardPeriod(typeof params.period === "string" ? params.period : undefined);
  const [repositories, result, unscopedJobs, unscopedTelemetry] = await Promise.all([
    getRepositories(),
    getPagedEvents(repo, getListQuery(params)),
    repo ? Promise.resolve(null) : getJobs(),
    repo ? Promise.resolve(null) : getRequestTelemetry(period),
  ]);
  const selected = repo ? repositories.find((r) => r.slug === repo) : undefined;
  const [jobLists, telemetry] = repo
    ? await Promise.all([
        selected ? getJobs(selected.slug).then((list) => [list]) : Promise.resolve([] as (Awaited<ReturnType<typeof getJobs>>)[]),
        getRequestTelemetry(period, selected?.id ?? 0),
      ])
    : [[unscopedJobs], unscopedTelemetry];
  const jobs = [...new Map(jobLists.flatMap((list) => (list ?? []).slice(0, 10)).map((j) => [j.id, j])).values()];
  const events = result.items;
  const hasQuery = Boolean(params.q || params.status || params.page || params.page_size);
  const tab = pickTab(VIEWS, typeof params.view === "string" ? params.view : undefined);
  const jobsFailed = jobs.filter((j) => JOB_FAILED.has(j.status)).length;
  const jobsRunning = jobs.filter((j) => JOB_RUNNING.has(j.status)).length;
  const jobsUnread = !jobs.length && jobLists.some((list) => list === null);
  const requestsTone: TabTone | undefined = !telemetry ? "warn" : telemetry.failed ? "warn" : telemetry.total ? "info" : undefined;
  const jobsTone: TabTone | undefined = jobsUnread ? "warn" : jobsFailed ? "bad" : jobsRunning ? "info" : jobs.length ? "ok" : undefined;
  return <>
    <ActivityViewHash views={VIEWS} />
    <PageTabs
      label="Activity views"
      path="/activity"
      params={{ repo, period: typeof params.period === "string" ? params.period : undefined }}
      current={tab}
      tabs={[
        { id: "requests", label: "Requests", badge: !telemetry ? "unavailable" : telemetry.failed ? `${telemetry.failed.toLocaleString("en-US")} failed` : telemetry.total.toLocaleString("en-US"), tone: requestsTone },
        { id: "jobs", label: "Jobs", badge: jobsUnread ? "unavailable" : jobsFailed ? `${jobsFailed} failed` : jobsRunning ? `${jobsRunning} running` : jobs.length, tone: jobsTone },
        { id: "events", label: "Events", badge: result.available === false ? "unavailable" : result.paging.total.toLocaleString("en-US"), tone: result.available === false ? "warn" : result.paging.total ? "info" : undefined },
      ]}
    />
    <ViewPane id={tab}>
    {tab === "requests" && <Panel id="requests" title="Agent requests" desc="The latest search and context calls, retries included. Handling time is measured on the server only." actions={<nav className="segmented" aria-label="Request period">{(["24h", "7d", "30d"] as const).map((p) => <Link key={p} scroll={false} aria-current={p === period ? "page" : undefined} href={`/activity?${new URLSearchParams({ ...(repo ? { repo } : {}), period: p })}`}>{p}</Link>)}</nav>} flush>
      <p className="text-faint">Query capture is {telemetry?.query_capture_enabled ? "enabled for future attempts" : "off unless enabled by the operator"}. Older queries cannot be recovered. Configure it in <Link className="link" href="/settings#activity">Settings → Activity history</Link>.</p>
      <RequestHistory telemetry={telemetry} projects={repositories} repositoryId={selected?.id} />
    </Panel>}
    {tab === "jobs" && <Panel id="jobs" title="Background jobs" desc="Indexing and checks Brain ran recently. A failed job shows its recorded error." flush>
      <BackgroundJobs jobs={jobs} unavailable={jobLists.some((list) => list === null)} />
    </Panel>}
    {tab === "events" && <Panel id="events" title="Event log" desc="Other events Brain recorded, newest first." flush>
      {result.available === false ? <EmptyState title="Event log unavailable" body="Brain didn’t return its event log just now. Reload in a moment." /> : events.length || hasQuery ? <Ledger events={events} paging={result.paging} /> : <EmptyState title="No recorded events" body="The event log is empty. Background job history is in the Jobs tab." />}
    </Panel>}
    </ViewPane>
  </>;
}
