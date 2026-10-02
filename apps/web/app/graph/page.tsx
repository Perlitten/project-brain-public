import { CodeGraph } from "@/components/CodeGraph";
import { Term } from "@/components/Term";
import { Chip, EmptyState, PageHead, Panel, Table } from "@/components/ui";
import { getModuleEdges } from "@/lib/data";

export const metadata = { title: "Code map" };

export default async function Graph() {
  const edges = await getModuleEdges();
  const violations = edges.filter((e) => e.violation).length;
  const sorted = [...edges].sort((a, b) => Number(b.violation) - Number(a.violation) || b.weight - a.weight);
  return (
    <>
      <PageHead
        eyebrow="Code map"
        title="Which parts of the code depend on which"
        lede={
          <>
            Each box is a part of your project; each line means one part uses another. Thicker lines mean more
            connections. A red, moving line breaks a rule your team agreed on — <Term k="coupling">tangled modules</Term>.
          </>
        }
      />
      {edges.length === 0 ? (
        <Panel id="map" title="Module map">
          <EmptyState
            title="No code map yet"
            body="Brain hasn’t reported how the parts of this code depend on each other. The map appears after the code graph is built."
          />
        </Panel>
      ) : (
      <>
      <Panel id="map" title="Module map" desc="Read left to right: entry points, then core logic, then storage." flush>
        <div className="graph-wrap">
          <CodeGraph edges={edges} />
        </div>
        <p className="graph-legend">
          <span>
            <i aria-hidden="true" /> normal dependency
          </span>
          <span>
            <i className="bad" aria-hidden="true" /> breaks a rule
          </span>
        </p>
      </Panel>
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
          columns={[
            { head: "This part", cell: (e) => <span className="mono">{e.from}</span> },
            { head: "uses", cell: (e) => <span className="mono">{e.to}</span> },
            { head: "Connections", cell: (e) => e.weight, align: "right" },
            { head: "Status", cell: (e) => (e.violation ? <Chip tone="bad">breaks a rule</Chip> : <Chip tone="ok">fine</Chip>) },
          ]}
        />
      </Panel>
      </>
      )}
    </>
  );
}
