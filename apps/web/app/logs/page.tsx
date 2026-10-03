import { EmptyState, Ledger, PageHead, Panel } from "@/components/ui";
import { getEvents } from "@/lib/data";

export const metadata = { title: "Activity" };

export default async function Activity() {
  const events = await getEvents();
  return <>
    <PageHead eyebrow="Activity" title="Recent activity" lede="Events recorded by Brain, newest first." />
    <Panel id="events" title="Activity" flush>
      {events.length ? <Ledger events={events} /> : <EmptyState title="No activity yet" body="Brain has not reported any recent events." />}
    </Panel>
  </>;
}
