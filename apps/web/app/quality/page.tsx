import Link from "next/link";
import { SectionTabs } from "@/components/SectionNav";
import { PageTabs, pickTab } from "@/components/PageTabs";
import { EmptyState, PageHead, Panel, Table } from "@/components/ui";
import { getPagedReports } from "@/lib/data";
import { getListQuery, listQueryParams } from "@/lib/list-query";
import { startBenchmark } from "@/lib/actions/jobs";
import { RunButton } from "@/components/act";
import { getQualityEvidence } from "@/lib/dashboard";
import { QualityEvidence } from "@/components/QualityEvidence";

export const metadata = { title: "Quality" };

const VIEWS = ["benchmark", "reports"] as const;

// The benchmark answers "does Brain find the right code?", and every saved
// report is the evidence behind that answer. They were two screens where the
// first one's only job was to link to the second, so they are one screen with
// two tabs; /reports still resolves here.
export default async function Quality({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const tab = pickTab(VIEWS, typeof params.view === "string" ? params.view : undefined);
  const query = getListQuery(params);
  const hasQuery = Boolean(params.q || params.status || params.page || params.page_size);
  const [result, evidence] = await Promise.all([
    getPagedReports(tab === "reports" ? query : {}),
    getQualityEvidence(),
  ]);
  const reports = result.items;
  const listParams = listQueryParams(query);
  const back = `/quality?view=reports${listParams ? `&${listParams}` : ""}`;
  return (
    <>
      <PageHead
        title="Quality"
        lede="Whether Brain finds the right code. Measured by asking questions whose right answers are known, not from live traffic — live request success is on Activity."
        actions={<RunButton action={startBenchmark} label="Run the test again" icon="play" title="Run search benchmark" />}
      />
      <SectionTabs section="quality" active="/quality" />
      <PageTabs
        label="Benchmark or saved reports"
        path="/quality"
        current={tab}
        tabs={[
          { id: "benchmark", label: "Benchmark" },
          { id: "reports", label: "Reports", badge: result.paging.total },
        ]}
      />
      {tab === "benchmark" ? (
        <Panel
          id="benchmark"
          title="Search benchmark"
          desc="The latest saved test run. Each result is tied to the Brain build it measured."
          actions={reports.length ? <Link className="link" href="/quality?view=reports">All {reports.length} saved reports</Link> : undefined}
        >
          <QualityEvidence evidence={evidence} />
        </Panel>
      ) : (
        <Panel id="reports" title="Saved reports" desc="Evaluations and checks recorded across the Brain service." flush>
          {reports.length || hasQuery ? (
            <Table
              paging={result.paging}
              filter={{ search: (r) => `${r.title} ${r.id}`, noun: ["report", "reports"] }}
              caption="Reports"
              rows={reports}
              rowKey={(r) => r.id}
              columns={[
                { head: "Report", cell: (r) => <Link className="link" href={`/reports/${encodeURIComponent(r.id)}?back=${encodeURIComponent(back)}`}>{r.title}</Link> },
                { head: "Kind", cell: (r) => r.kind },
                { head: "Created", cell: (r) => r.createdAt },
                { head: "Size", cell: (r) => r.size, align: "right" },
              ]}
            />
          ) : (
            <EmptyState title="No reports yet" body="Brain has not reported any saved evaluations or checks." />
          )}
        </Panel>
      )}
    </>
  );
}
