import { ClampText } from "./ClampText";
import { TraceLink } from "./TraceLink";
import { utcTime } from "@/lib/time";
import { Chip, EmptyState, Table } from "./ui";
import { measuredNumber, type RequestTelemetry } from "@/lib/dashboard";

export function RequestHistory({ telemetry, projects = [], repositoryId }: { telemetry: RequestTelemetry | null; projects?: { id: number; name: string }[]; repositoryId?: number }) {
  if (!telemetry) return <EmptyState title="Request measurements unavailable" body="This Brain server did not return request telemetry. Check the API connection and collector status." />;
  if (!telemetry.recent_requests.length) return <EmptyState title="No recorded requests in this period" body="Search and context attempts appear here after collection starts. Earlier periods are unmeasured." />;
  return <Table caption="Latest 20 search and context request attempts" rows={telemetry.recent_requests} rowKey={(r) => `${r.request_id}-${r.recorded_at}`} columns={[
    { head: "Recorded, UTC", cell: (r) => utcTime(r.recorded_at, true) },
    { head: "Request", cell: (r) => <><b>{r.operation}</b><span className="sub">{r.surface.toUpperCase()} · {r.repository_name ?? projects.find(p => p.id === r.repository_id)?.name ?? (r.repository_id ? "Project unavailable" : "Unattributed project")}</span></> },
    { head: "Query", cell: (r) => r.query_text ? <ClampText text={r.query_text} className="request-query" lines={3} label="query" /> : <span className="text-faint">Query not recorded</span> },
    { head: "Identity", cell: (r) => r.identity.endsWith(":unattributed") ? "Not attributed" : r.identity },
    { head: "Outcome", cell: (r) => <Chip tone={r.outcome === "success" ? "ok" : ["error", "failed", "timeout", "cancelled"].includes(r.outcome) ? "bad" : "warn"}>{r.outcome}</Chip> },
    { head: "Handling time", cell: (r) => measuredNumber(r.latency_ms, " ms"), align: "right" },
    { head: "Trace", cell: (r) => r.request_id ? <TraceLink id={r.request_id} repositoryId={repositoryId} /> : "not recorded" },
  ]} />;
}
