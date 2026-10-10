import { Term } from "@/components/Term";
import { Chip, EmptyState, PageHead, Panel, Table, fmt } from "@/components/ui";
import { SectionTabs } from "@/components/SectionNav";
import { getMcpTools } from "@/lib/data";
import type { McpTool } from "@/lib/types";

export const metadata = { title: "Agent tools" };

const health = { ok: "healthy", warn: "slow", bad: "failing", info: "not measured", idle: "unused" } as const;

export default async function Mcp() {
  const tools = await getMcpTools();
  const tracked = tools.some((t) => t.calls24h !== undefined || t.errorRate !== undefined);
  const troubled = tools.filter((t) => t.status === "warn" || t.status === "bad").length;
  return (
    <>
      <PageHead
        title="Agent tools"
        status={
          tools.length === 0
            ? { tone: "idle", text: "No agent tools reported" }
            : troubled
              ? { tone: "warn", text: `${troubled} tool${troubled > 1 ? "s are" : " is"} slow or failing` }
              : tracked
                ? { tone: "ok", text: "All agent tools are healthy" }
                : { tone: "info", text: <>{tools.length} agent tools available<small>Their calls are not measured yet.</small></> }
        }
        lede={
          <>
            Agents talk to Brain through tools over <Term k="mcp">MCP</Term>. {tracked
              ? "This screen shows recorded calls, response times and errors."
              : "These tools are available. Usage, response times and errors are not measured yet."}
          </>
        }
      />
      <SectionTabs section="settings" active="/mcp" />
      <Panel id="tools" title="Tools" desc={tracked ? "Last 24 hours. An error rate above 5% is shown in red." : "What your agent can ask Brain for. This server doesn’t count calls yet, so usage and errors aren’t shown."} flush>
        {tools.length === 0 ? (
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
                        <span className={t.errorRate > 0.05 ? "text-bad" : t.errorRate > 0.02 ? "text-warn" : undefined}>
                          {(t.errorRate * 100).toFixed(1)}%
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
      </Panel>
    </>
  );
}
