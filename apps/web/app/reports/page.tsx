import { EmptyState, PageHead, Panel, Table } from "@/components/ui";
import { getReports } from "@/lib/data";

export const metadata = { title: "Reports" };

export default async function Reports() {
  const reports = await getReports();
  return <>
    <PageHead eyebrow="Reports" title="Saved reports" lede="Evaluations and checks recorded by Brain." />
    <Panel id="reports" title="Reports" flush>
      {reports.length ? <Table caption="Reports" rows={reports} rowKey={r => r.id} columns={[
        { head: "Report", cell: r => r.title }, { head: "Kind", cell: r => r.kind },
        { head: "Created", cell: r => r.createdAt }, { head: "Size", cell: r => r.size, align: "right" },
      ]} /> : <EmptyState title="No reports yet" body="Brain has not reported any saved evaluations or checks." />}
    </Panel>
  </>;
}
