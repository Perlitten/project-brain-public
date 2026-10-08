import { Chip, EmptyState, Table } from "./ui";
import { measuredNumber, type RequestTelemetry } from "@/lib/dashboard";

export function RequestHistory({ telemetry }: { telemetry: RequestTelemetry | null }) {
  if (!telemetry) return <EmptyState title="Request measurements unavailable" body="This Brain server did not return request telemetry. Check the API connection and collector status." />;
  if (!telemetry.recent_requests.length) return <EmptyState title="No recorded requests in this period" body="Search and context attempts appear here after collection starts. Earlier periods are unmeasured." />;
  return <Table caption="Latest 20 search and context request attempts" rows={telemetry.recent_requests} rowKey={(r) => `${r.request_id}-${r.recorded_at}`} columns={[
    { head: "Recorded, UTC", cell: (r) => new Date(r.recorded_at).toLocaleString("en-GB", { timeZone: "UTC", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) },
    { head: "Request", cell: (r) => <><b>{r.operation}</b><span className="sub">{r.surface.toUpperCase()} · {r.repository_id ? `project #${r.repository_id}` : "unattributed project"}</span></> },
    { head: "Identity", cell: (r) => r.identity.endsWith(":unattributed") ? "Not attributed" : r.identity },
    { head: "Outcome", cell: (r) => <Chip tone={r.outcome === "success" ? "ok" : ["error", "failed", "timeout", "cancelled"].includes(r.outcome) ? "bad" : "warn"}>{r.outcome}</Chip> },
    { head: "Handling time", cell: (r) => measuredNumber(r.latency_ms, " ms"), align: "right" },
    { head: "Trace", cell: (r) => <code title={r.request_id ?? undefined}>{r.request_id?.slice(0, 12) ?? "not recorded"}</code> },
  ]} />;
}
