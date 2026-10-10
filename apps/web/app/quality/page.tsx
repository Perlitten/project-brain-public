import Link from "next/link";
import { SectionNav } from "@/components/SectionNav";
import { EmptyState, PageHead, Panel, Table } from "@/components/ui";
import { getReports } from "@/lib/data";
import { startBenchmark } from "@/lib/actions/jobs";
import { RunButton } from "@/components/act";
import { getQualityEvidence } from "@/lib/dashboard";
import { QualityEvidence } from "@/components/QualityEvidence";

export const metadata = { title: "Quality" };

export default async function Quality() {
  const [reports, evidence] = await Promise.all([getReports(), getQualityEvidence()]);
  return (
    <>
      <PageHead title="Quality" lede="How well Brain finds the right code, measured by saved benchmarks. Live request success is on Activity." actions={<RunButton action={startBenchmark} label="Run benchmark" icon="play" title="Run search benchmark" />} />
      <SectionNav label="Quality views" items={[{ href: "/quality", label: "Quality", active: true }, { href: "/reports", label: "Reports" }, { href: "/reranker", label: "Reranker" }]} />
      <Panel id="benchmark" title="Retrieval benchmark" desc="Expected evidence on a fixed question set. Saved results are tied to their measured release, independently of live request traffic."><QualityEvidence evidence={evidence} /></Panel>
      <Panel id="evaluations" title="Saved evaluations" desc={`${reports.length} saved report${reports.length === 1 ? "" : "s"} currently available. Open a report for its measured sample and metrics.`} flush>
        {reports.length === 0 ? <EmptyState title="No saved evaluations" body="Run an evaluation or save a report before reviewing retrieval quality evidence." /> : (
          <Table caption="Saved evaluations" rows={reports} rowKey={(r) => r.id} columns={[
            { head: "Report", cell: (r) => <Link className="link" href={`/reports/${encodeURIComponent(r.id)}`}>{r.title}</Link> },
            { head: "Kind", cell: (r) => r.kind }, { head: "Recorded", cell: (r) => r.createdAt }, { head: "Size", cell: (r) => r.size, align: "right" },
          ]} />
        )}
      </Panel>
    </>
  );
}
