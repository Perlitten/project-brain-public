import { EmptyState, Ledger, PageHead, Panel } from "@/components/ui";
import { getPagedEvents } from "@/lib/data";
import { getListQuery } from "@/lib/list-query";

export const metadata = { title: "Activity" };

export default async function Activity({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const result = await getPagedEvents(typeof params.repo === "string" ? params.repo : undefined, getListQuery(params));
  const events = result.items;
  const hasQuery = Boolean(params.q || params.status || params.page || params.page_size);
  return <>
    <PageHead eyebrow="Activity" title="Recent activity" lede="Events recorded by Brain, newest first." />
    <Panel id="events" title="Activity" flush>
      {events.length || hasQuery ? <Ledger events={events} paging={result.paging} /> : <EmptyState title="No activity yet" body="Brain has not reported any recent events." />}
    </Panel>
  </>;
}
