import Link from "next/link";
import { CodeBlock } from "@/components/act";
import { mcpUrl } from "@/lib/mcp-address";
import { Term } from "@/components/Term";
import { toolSummary } from "@/lib/tool-health";
import { Chip, EmptyState, PageHead, Panel, Table, fmt } from "@/components/ui";
import { SectionTabs } from "@/components/SectionNav";
import { getMcpTools } from "@/lib/data";
import { getAgentTarget } from "@/lib/setup-view";
import type { McpTool } from "@/lib/types";
import { getCapabilities } from "@/lib/capabilities";
import { PageTabs, ViewPane, pickTab, type TabTone } from "@/components/PageTabs";

export const metadata = { title: "Agent tools" };

const VIEWS = ["tools", "connect"] as const;
const health = { ok: "healthy", warn: "slow", bad: "failing", info: "not measured", idle: "unused" } as const;

export default async function Mcp({ searchParams }: { searchParams: Promise<{ view?: string }> }) {
  const [read, agent, capabilities, query] = await Promise.all([getMcpTools(), getAgentTarget(), getCapabilities(), searchParams]);
  const tools = read ?? [];
  const httpMcp = capabilities.supported.includes("mcp_http");
  const tracked = tools.some((t) => t.calls24h !== undefined || t.errorRate !== undefined);
  const tab = pickTab(VIEWS, query.view);
  const failing = tools.filter((t) => t.status === "bad").length;
  const slow = tools.filter((t) => t.status === "warn").length;
  const toolsTone: TabTone | undefined = read === null ? "warn" : failing ? "bad" : slow ? "warn" : !tools.length ? undefined : tracked && tools.every((t) => t.status === "ok") ? "ok" : "info";
  const toolsBadge = read === null ? "unavailable" : failing ? `${failing} failing` : slow ? `${slow} slow` : tools.length;
  return (
    <>
      <SectionTabs section="logs" active="/mcp" />
      <PageHead
        title="Agent tools"
        status={
          read === null
            ? { tone: "warn", text: "Tool status unavailable" }
            : toolSummary(tools)
        }
        lede={
          <>
            Agents talk to Brain through tools over <Term k="mcp">MCP</Term>. {tracked
              ? "This screen shows recorded calls, response times and errors."
              : "These tools are available. Usage, response times and errors are not measured yet."}
          </>
        }
      />
      <PageTabs
        label="Agent tool views"
        path="/mcp"
        current={tab}
        tabs={[
          { id: "tools", label: "Tools", badge: toolsBadge, tone: toolsTone },
          { id: "connect", label: "Connect", badge: httpMcp ? "ready" : "stdio only", tone: httpMcp ? "ok" : "info" },
        ]}
      />
      <ViewPane id={tab}>
      {tab === "connect" && <Panel
        id="connect"
        title="Connect an agent"
        desc="Agents reach these tools at one address, each with its own key. Keys are created and revoked in Access."
        actions={<Link className="btn btn--primary btn--sm" href="/admin?new=agent#new-identity">Create an agent key</Link>}
      >
        {httpMcp
          ? <CodeBlock label="MCP address" code={mcpUrl(agent.publicApiUrl)} />
          : <EmptyState title="HTTP MCP is disabled" body="This Brain server does not expose /mcp. Use the local stdio bridge configuration in Setup instead." action={<Link className="btn btn--neutral" href="/setup#agent">Open agent setup</Link>} />}
      </Panel>}
      {tab === "tools" && <Panel id="tools" title="Tools" desc={tracked ? "Last 24 hours. Errors above 5% are red; p95 responses over 2 seconds are slow. Unused tools have no verified health." : "What your agent can ask Brain for. This server doesn’t count calls yet, so usage and errors aren’t shown."} flush>
        {read === null ? (
          <EmptyState title="Tool status unavailable" body="Brain didn’t answer the agent-tool status request just now. Reload in a moment." />
        ) : tools.length === 0 ? (
          <EmptyState title="No tools to show" body="Brain didn’t report any agent tools. Check that its MCP server is running." />
        ) : (
        <Table
          caption="Agent tools"
          rows={tools}
          rowKey={(t) => t.name}
          filter={{
            search: (t) => `${t.name} ${t.surface}`,
            facet: tracked ? { label: "Health", of: (t) => t.status, values: (["bad", "warn", "ok", "info", "idle"] as const).map((v) => ({ value: v, label: health[v] })) } : undefined,
            noun: ["tool", "tools"],
          }}
          columns={[
            {
              head: "Tool",
              cell: (t) => (
                <>
                  <span className="mono">{t.name}</span>
                  <span className="sub">{t.surface === "remote" ? "over the network" : "on this machine"}</span>
                </>
              ),
            },
            // Usage columns only when the server measures them; otherwise every cell would read "—".
            ...(tracked
              ? [
                  { head: "Calls", cell: (t: McpTool) => (t.calls24h === undefined ? "—" : fmt(t.calls24h)), align: "right" as const },
                  { head: <Term k="p95">Slow answers</Term>, cell: (t: McpTool) => (t.p95ms === undefined ? "—" : `${fmt(t.p95ms)}ms`), align: "right" as const },
                  {
                    head: "Errors",
                    cell: (t: McpTool) =>
                      t.errorRate === undefined ? (
                        "—"
                      ) : (
                        <span className="task-result">
                          <span className={t.errorRate > 0.05 ? "text-bad" : t.errorRate > 0.02 ? "text-warn" : undefined}>
                            {(t.errorRate * 100).toFixed(1)}%
                          </span>
                          {t.errorRate > 0.05 && <Link className="link sub" href="/activity#requests" title="Recent requests, newest first — the failing calls are there">recent calls</Link>}
                        </span>
                      ),
                    align: "right" as const,
                  },
                  { head: "Health", cell: (t: McpTool) => <Chip tone={t.status}>{health[t.status]}</Chip> },
                ]
              : []),
          ]}
        />
        )}
      </Panel>}
      </ViewPane>
    </>
  );
}
