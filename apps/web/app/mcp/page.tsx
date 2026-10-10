import { Term } from "@/components/Term";
import { Chip, EmptyState, PageHead, Panel, Table, fmt } from "@/components/ui";
import { SectionNav } from "@/components/SectionNav";
import { getMcpTools } from "@/lib/data";

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
      <SectionNav label="Settings views" items={[{ href: "/settings", label: "Settings" }, { href: "/admin", label: "Access" }, { href: "/mcp", label: "Agent tools", active: true }]} />
      <Panel id="tools" title="Tools" desc={tracked ? "Last 24 hours. An error rate above 5% is shown in red." : "Brain lists its tools but does not count their calls yet."} flush>
        {tools.length === 0 ? (
          <EmptyState title="No tools to show" body="Brain didn’t report any agent tools. Check that its MCP server is running." />
        ) : (
        <Table
          caption="Agent tools"
          rows={tools}
          rowKey={(t) => t.name}
          filter={{
            search: (t) => `${t.name} ${t.surface}`,
            facet: { label: "Health", of: (t) => t.status, values: (["bad", "warn", "ok", "info", "idle"] as const).map((v) => ({ value: v, label: health[v] })) },
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
            { head: "Calls", cell: (t) => (t.calls24h === undefined ? "—" : fmt(t.calls24h)), align: "right" },
            { head: <Term k="p95">Slow answers</Term>, cell: (t) => (t.p95ms === undefined ? "—" : `${fmt(t.p95ms)}ms`), align: "right" },
            {
              head: "Errors",
              cell: (t) =>
                t.errorRate === undefined ? (
                  "—"
                ) : (
                  <span className={t.errorRate > 0.05 ? "text-bad" : t.errorRate > 0.02 ? "text-warn" : undefined}>
                    {(t.errorRate * 100).toFixed(1)}%
                  </span>
                ),
              align: "right",
            },
            { head: "Health", cell: (t) => <Chip tone={t.status}>{health[t.status]}</Chip> },
          ]}
        />
        )}
      </Panel>
    </>
  );
}
