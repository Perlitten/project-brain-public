import { adminOnlyPage } from "@/components/AdminOnly";
import { capabilityGate } from "@/components/CapabilityGate";
import { EmptyState, PageHead, Panel } from "@/components/ui";
import { brainRead } from "@/lib/api";
export const metadata = { title: "Findings" };
export default async function FindingCode({ params }: { params: Promise<{ id: string }> }) {
  const gate = await adminOnlyPage("Findings") ?? await capabilityGate("finding_actions", "Findings");
  if (gate) return gate;
  const { id } = await params;
  const result = await brainRead<{ repository: string; path: string; line: number; revision: string | null; chunks: { start_line: number | null; end_line: number | null; content: string }[] }>(`/insights/${encodeURIComponent(id)}/code`, { fresh: true });
  if (result.ok && (!Array.isArray(result.data.chunks) || result.data.chunks.some(chunk => !chunk || typeof chunk.content !== "string"))) return <><PageHead title="Findings" /><EmptyState title="Unreadable code evidence" body="The API returned an unexpected evidence format. Retry or update the server and dashboard together." /></>;
  return <><PageHead title="Findings" lede="File-bound evidence from the repository index. This is an indexed snapshot, not the live checkout." />
    {!result.ok ? <EmptyState title={result.status === 403 ? "Project source access required" : result.status === 404 ? "Indexed file evidence unavailable" : "Code evidence unavailable"} body="The finding needs an explicit repository and indexed file. Historical module-only findings cannot identify a source file safely." /> : <Panel title={`${result.data.repository} · ${result.data.path}:${result.data.line}`} desc={`Indexed revision: ${result.data.revision || "not recorded"}`}>
      {result.data.chunks.map((chunk, i) => <div key={i}><p>Lines {chunk.start_line ?? "?"}–{chunk.end_line ?? "?"}</p><pre>{chunk.content}</pre></div>)}
    </Panel>}
  </>;
}
