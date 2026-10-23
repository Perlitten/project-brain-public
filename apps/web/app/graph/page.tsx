import Link from "next/link";
import { CodeGraph } from "@/components/CodeGraph";
import { SectionTabs } from "@/components/SectionNav";
import { PageTabs, pickTab } from "@/components/PageTabs";
import { Term } from "@/components/Term";
import { Chip, EmptyState, PageHead, Panel, Table, type Column } from "@/components/ui";
import { getModuleEdges } from "@/lib/data";
import type { ModuleEdge } from "@/lib/types";

export const metadata = { title: "Code map" };

const VIEWS = ["map", "connections"] as const;

export default async function Graph({ searchParams }: { searchParams: Promise<{ repo?: string; view?: string; module?: string }> }) {
  const { repo, view, module } = await searchParams;
  const tab = pickTab(VIEWS, view);
  const read = await getModuleEdges(repo);
  const edges = read ?? [];
  const violations = edges.filter((e) => e.violation).length;
  const sorted = [...edges].sort((a, b) => Number(b.violation) - Number(a.violation) || b.weight - a.weight);
  // Rule breaks and the strongest links up front; the long tail is paged.
  // A red line is an architecture boundary check, reported as a drift finding;
  // the edge does not say which check, so the link opens those findings.
  const drift = `/insights?${new URLSearchParams({ ...(repo ? { repo } : {}), q: "Drift" })}`;
  const columns: Column<ModuleEdge>[] = [
    { head: "This part", cell: (e) => <span className="mono">{e.from}</span> },
    { head: "uses", cell: (e) => <span className="mono">{e.to}</span> },
    { head: "Connections", cell: (e) => e.weight, align: "right" },
    {
      head: "Status",
      cell: (e) =>
        e.violation ? (
          <>
            <Chip tone="bad">breaks a rule</Chip>
            <Link className="sub link" href={drift}>See rule-break findings</Link>
          </>
        ) : (
          <Chip tone="ok">fine</Chip>
        ),
    },
  ];
  return (
    <>
      <SectionTabs section="projects" active="/graph" />
      <PageHead
        title="Code map"
        lede={
          <>
            Each box is a part of your project; each line means one part uses another. Thicker lines mean more
            connections. A red line breaks a rule you agreed on — <Term k="coupling">tangled modules</Term>.
          </>
        }
      />
      {read === null ? (
        <Panel id="map" title="Module map">
          <EmptyState title="Code map unavailable" body="Brain didn’t return the module graph just now — its graph store may be down. Reload in a moment." />
        </Panel>
      ) : edges.length === 0 ? (
        <Panel id="map" title="Module map">
          <EmptyState
            title="No code map yet"
            body="Brain hasn’t reported how the parts of this code depend on each other. The map appears after the code graph is built."
          />
        </Panel>
      ) : (
        <>
          <PageTabs
            label="Code map views"
            path="/graph"
            params={{ repo, module }}
            current={tab}
            tabs={[
              { id: "map", label: "Map" },
              { id: "connections", label: "Connections", badge: violations ? `${violations} break${violations > 1 ? "" : "s"} a rule` : edges.length, alarm: violations > 0 },
            ]}
          />
          {tab === "map" ? (
            <Panel id="map" title="Module map" desc="Start with areas, then expand or focus the connections you need." flush>
              <div className="code-map-wrap">
                <CodeGraph key={module ?? ""} edges={edges} module={module} />
              </div>
              <p className="graph-legend">
                <span>
                  <i aria-hidden="true" /> normal dependency
                </span>
                <span>
                  <i className="bad" aria-hidden="true" /> breaks a rule
                </span>
                {violations > 0 && <Link className="link" href={drift}>See rule-break findings</Link>}
              </p>
            </Panel>
          ) : (
            <Panel
              id="edges"
              title={violations ? `${violations} connection${violations > 1 ? "s" : ""} break${violations > 1 ? "" : "s"} a rule` : "All connections follow the rules"}
              spine={violations ? "bad" : "ok"}
              flush
            >
              <Table
                caption="Module connections"
                rows={sorted}
                rowKey={(e) => `${e.from}-${e.to}`}
                columns={columns}
                filter={{
                  search: (e) => `${e.from} ${e.to}`,
                  facet: { label: "Status", of: (e) => (e.violation ? "bad" : "ok"), values: [{ value: "bad", label: "breaks a rule" }, { value: "ok", label: "fine" }] },
                  noun: ["connection", "connections"],
                }}
              />
            </Panel>
          )}
        </>
      )}
    </>
  );
}
