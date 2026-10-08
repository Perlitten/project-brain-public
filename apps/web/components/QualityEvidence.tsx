import type { QualityEvidence as Evidence } from "@/lib/dashboard";
import { measuredNumber } from "@/lib/dashboard";
import { Chip, EmptyState } from "./ui";
import "./quality-evidence.css";

export function QualityEvidence({ evidence, compact = false }: { evidence: Evidence | null; compact?: boolean }) {
  if (!evidence) return <EmptyState title="Benchmark measurements unavailable" body="Brain did not return the saved benchmark snapshot. Open a saved report below to inspect its evidence." />;
  const { source, search, context, quality_limits: limits } = evidence;
  const searchRelease = source.provenance.find((p) => p.artifact.startsWith("retrieval-"))?.measured_build_sha ?? source.build_sha;
  return <div className="quality-evidence">
    <div className="quality-evidence__scope"><Chip tone="info">Saved benchmark</Chip><span>{source.search_sample_count} search questions · {context.normal_count} supported context fixtures</span></div>
    <div className="quality-evidence__metrics"><div><span>Hit@5</span><strong className="num">{measuredNumber(search.hit5_any_pct, "%")}</strong><small>At least one expected file in the first 5 results</small></div><div><span>Mean reciprocal rank</span><strong className="num">{search.mrr_any.toFixed(3)}</strong><small>Rank of the first expected file</small></div>{!compact && <div><span>Context evidence recall</span><strong className="num">{measuredNumber(context.mean_recall * 100, "%")}</strong><small>{context.normal_count} supported fixtures; not real-task completion</small></div>}</div>
    <p className="quality-evidence__meta">Snapshot {evidence.generated_at ? new Date(evidence.generated_at).toLocaleString("en-GB", { timeZone: "UTC" }) : "at an unknown time"} UTC · search release <code>{searchRelease.slice(0, 7)}</code> · context release <code>{source.build_sha.slice(0, 7)}</code> · corpus <code>{source.corpus_sha256.slice(0, 12)}</code>. Historical evidence; the dashboard period does not filter these tests.</p>
    {!compact && <><div className="quality-evidence__details"><p>Hit@1 <b>{measuredNumber(search.hit1_any_pct, "%")}</b> · Hit@3 <b>{measuredNumber(search.hit3_any_pct, "%")}</b></p><p>Context fixture noise: <b>{measuredNumber(context.mean_fixture_noise_ratio * 100, "%")}</b>. This fixture-specific measure is not a relevance score.</p><p>Unsupported-query outcome: <b>{context.adversarial_status}</b> · {source.context_fixture_count - context.normal_count} adversarial fixture.</p></div><p className="quality-evidence__limit">Task evaluation: {limits.native_tasks_passed} / {limits.native_task_count} tasks passed; precision {measuredNumber(limits.native_precision * 100, "%")}, recall {measuredNumber(limits.native_recall * 100, "%")}. A small benchmark does not establish general usefulness or token savings.</p></>}
  </div>;
}
