import Link from "next/link";
import { SectionNav } from "@/components/SectionNav";
import { EmptyState, PageHead, Panel, Table } from "@/components/ui";
import { getPagedReports } from "@/lib/data";
import { getListQuery, listQueryParams } from "@/lib/list-query";

export const metadata = { title: "Reports" };

export default async function Reports({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const query = getListQuery(params);
  const result = await getPagedReports(query);
  const listParams = listQueryParams(query);
  const back = `/reports${listParams ? `?${listParams}` : ""}`;
  const hasQuery = Boolean(params.q || params.status || params.page || params.page_size);
  const reports = result.items;
  return <>
    <PageHead eyebrow="Reports" title="Saved reports" lede="Evaluations and checks recorded across the Brain service." />
    <SectionNav label="Quality views" items={[{ href: "/quality", label: "Quality" }, { href: "/reports", label: "Reports", active: true }, { href: "/reranker", label: "Reranker diagnostics" }]} />
    <Panel id="reports" title="Reports" flush>
      {reports.length || hasQuery ? <Table paging={result.paging} filter={{search: r => `${r.title} ${r.id}`, noun: ["report", "reports"]}} caption="Reports" rows={reports} rowKey={r => r.id} columns={[
        { head: "Report", cell: r => <Link className="link" href={`/reports/${encodeURIComponent(r.id)}?back=${encodeURIComponent(back)}`}>{r.title}</Link> }, { head: "Kind", cell: r => r.kind },
        { head: "Created", cell: r => r.createdAt }, { head: "Size", cell: r => r.size, align: "right" },
      ]} /> : <EmptyState title="No reports yet" body="Brain has not reported any saved evaluations or checks." />}
    </Panel>
  </>;
}
