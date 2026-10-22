import { adminOnlyPage } from "@/components/AdminOnly";
import { capabilityGate } from "@/components/CapabilityGate";
import { EmptyState, PageHead, Panel } from "@/components/ui";
import { brainRead } from "@/lib/api";
import { utcTime } from "@/lib/time";
export const metadata = { title: "Activity" };
export default async function RequestTrace({ params, searchParams }: { params: Promise<{ id: string }>; searchParams: Promise<{ repo?: string }> }) {
  const gate = await adminOnlyPage("Activity") ?? await capabilityGate("request_traces", "Activity");
  if (gate) return gate;
  const { id } = await params; const { repo } = await searchParams;
  if (repo && !/^\d+$/.test(repo)) return <><PageHead title="Activity" /><EmptyState title="Project not found" body="Select an existing project before opening this trace." /></>;
  const scope = repo && /^\d+$/.test(repo) ? `?repository_id=${repo}` : "";
  const result = await brainRead<{ request_id: string; observations: { operation: string; surface: string; outcome: string; recorded_at: string; identity: string; latency_ms: number; query_text?: string | null }[] }>(`/api/web/requests/${encodeURIComponent(id)}${scope}`, { fresh: true });
  if (result.ok && (!Array.isArray(result.data.observations) || result.data.observations.some(row => !row || typeof row.operation !== "string" || typeof row.recorded_at !== "string" || (row.query_text != null && typeof row.query_text !== "string")))) return <><PageHead title="Activity" /><EmptyState title="Unreadable request trace" body="The API returned an unexpected trace format. Retry or update the server and dashboard together." /></>;
  return <><PageHead title="Activity" lede="Recorded request trace, with the original observation times." /><Panel title={`Trace ${id}`}>
    {!result.ok ? <EmptyState title={result.status === 403 ? "Permission required" : result.status === 404 ? "Trace not found in this project" : "Trace unavailable"} body="Retry, check the selected project and verify that your credential can read its request history." /> : result.data.observations.map((row, i) => <article key={i}><h2>{row.operation} · {row.outcome}</h2><p>{utcTime(row.recorded_at, true)} · {row.identity} · {row.surface} · {row.latency_ms} ms</p><pre>{row.query_text || "Query not recorded"}</pre></article>)}
  </Panel></>;
}
