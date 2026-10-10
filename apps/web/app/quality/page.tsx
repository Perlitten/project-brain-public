import Link from "next/link";
import { SectionTabs } from "@/components/SectionNav";
import { PageHead, Panel } from "@/components/ui";
import { getReports } from "@/lib/data";
import { startBenchmark } from "@/lib/actions/jobs";
import { RunButton } from "@/components/act";
import { getQualityEvidence } from "@/lib/dashboard";
import { QualityEvidence } from "@/components/QualityEvidence";

export const metadata = { title: "Quality" };

// The benchmark answers "does Brain find the right code?". Every saved report
// lives on Reports; this page only points there instead of repeating the list.
export default async function Quality() {
  const [reports, evidence] = await Promise.all([getReports(), getQualityEvidence()]);
  return (
    <>
      <PageHead title="Quality" lede="Whether Brain finds the right code. Measured by asking questions whose right answers are known, not from live traffic — live request success is on Activity." actions={<RunButton action={startBenchmark} label="Run the test again" icon="play" title="Run search benchmark" />} />
      <SectionTabs section="quality" active="/quality" />
      <Panel id="benchmark" title="Search benchmark" desc="The latest saved test run. Each result is tied to the Brain build it measured." actions={reports.length ? <Link className="link" href="/reports">All {reports.length} saved reports</Link> : undefined}><QualityEvidence evidence={evidence} /></Panel>
    </>
  );
}
