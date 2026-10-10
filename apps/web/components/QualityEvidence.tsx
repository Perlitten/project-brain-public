import type { QualityEvidence as Evidence } from "@/lib/dashboard";
import { measuredNumber } from "@/lib/dashboard";
import { Chip, EmptyState } from "./ui";
import "./quality-evidence.css";

export function QualityEvidence({ evidence, compact = false }: { evidence: Evidence | null; compact?: boolean }) {
  if (!evidence) return <EmptyState title="Benchmark measurements unavailable" body="Brain did not return the saved benchmark snapshot. Earlier results stay available in saved reports." />;
  const { source, search, context, quality_limits: limits } = evidence;
  const searchRelease = source.provenance.find((p) => p.artifact.startsWith("retrieval-"))?.measured_build_sha ?? source.build_sha;
  return <div className="quality-evidence">
    <div className="quality-evidence__scope"><Chip tone="info">Saved test run</Chip><span>{source.search_sample_count} test questions with known right answers · {context.normal_count} test tasks</span></div>
    <div className="quality-evidence__metrics"><div><span>Right file in top 5</span><strong className="num">{measuredNumber(search.hit5_any_pct, "%")}</strong><small>Questions where a right file was among the first 5 results</small></div><div><span>Ranking score</span><strong className="num">{search.mrr_any.toFixed(3)}</strong><small>1.000 means the right file always came first; 0.500 means second on average</small></div>{!compact && <div><span>Context pack completeness</span><strong className="num">{measuredNumber(context.mean_recall * 100, "%")}</strong><small>Expected evidence found in packs for {context.normal_count} test tasks; not real-task success</small></div>}</div>
    <p className="quality-evidence__meta">Measured {evidence.generated_at ? new Date(evidence.generated_at).toLocaleString("en-GB", { timeZone: "UTC" }) + " UTC" : "at an unknown time"}. A past test run, not live traffic; the period selector does not change it.{!compact && <> Search build <code>{searchRelease.slice(0, 7)}</code> · context build <code>{source.build_sha.slice(0, 7)}</code> · test corpus <code>{source.corpus_sha256.slice(0, 12)}</code>.</>}</p>
    {!compact && <><div className="quality-evidence__details"><p>Right file first <b>{measuredNumber(search.hit1_any_pct, "%")}</b> · in top 3 <b>{measuredNumber(search.hit3_any_pct, "%")}</b></p><p>Unrelated material in test packs: <b>{measuredNumber(context.mean_fixture_noise_ratio * 100, "%")}</b>. Specific to these test tasks; not a relevance score.</p><p>Question Brain should decline: <b>{context.adversarial_status}</b> · {source.context_fixture_count - context.normal_count} such test.</p></div><p className="quality-evidence__limit">End-to-end tasks: {limits.native_tasks_passed} of {limits.native_task_count} passed; precision {measuredNumber(limits.native_precision * 100, "%")}, recall {measuredNumber(limits.native_recall * 100, "%")}. A small benchmark does not establish general usefulness or token savings.</p></>}
  </div>;
}
