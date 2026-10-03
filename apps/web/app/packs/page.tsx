import { Term } from "@/components/Term";
import { Chip, EmptyState, PageHead, Panel, Stat, Table, fmt } from "@/components/ui";
import { getContextPacks } from "@/lib/data";

export const metadata = { title: "Context packs" };

export default async function Packs() {
  const packs = await getContextPacks();
  const stale = packs.filter((p) => p.stale === true).length;
  const unknown = packs.filter((p) => p.stale === null).length;
  const state = (p: (typeof packs)[number]) => (p.stale === null ? "unknown" : p.stale ? "outdated" : "fresh");
  const avg = packs.length ? Math.round(packs.reduce((s, p) => s + p.tokens, 0) / packs.length) : 0;
  return (
    <>
      <PageHead
        eyebrow="Context packs"
        title="Briefings Brain prepared for AI agents"
        lede={
          <>
            Before an agent starts a task, Brain hands it a <Term k="pack">context pack</Term>: only the files, decisions and
            rules that matter. Agents read less and make fewer mistakes.
          </>
        }
      />
      <div className="stats">
        <Stat label="Briefings" value={String(packs.length)} note="prepared recently" />
        <Stat label="Average size" value={fmt(avg)} note="tokens, about ¾ of a word each" />
        {unknown === packs.length && packs.length > 0 ? (
          <Stat label="Outdated" value="—" note="unknown: these packs don’t record the commit they were built from" />
        ) : (
          <div className={stale ? "tone-warn" : "tone-ok"}>
            <Stat label="Outdated" value={String(stale)} note={unknown ? `the code changed after they were built · ${unknown} unknown` : "the code changed after they were built"} />
          </div>
        )}
      </div>
      <Panel id="packs" title="Recent briefings" flush>
        {packs.length === 0 ? (
          <EmptyState title="No briefings yet" body="Brain hasn’t prepared a context pack for any agent recently." />
        ) : (
        <Table
          caption="Context packs"
          rows={packs}
          rowKey={(p) => p.id}
          filter={{
            search: (p) => `${p.task} ${p.id} ${p.consumer}`,
            facet: { label: "State", of: state, values: [{ value: "outdated" }, { value: "fresh" }, { value: "unknown" }] },
            noun: ["pack", "packs"],
          }}
          columns={[
            {
              head: "Task",
              cell: (p) => (
                <>
                  <span>{p.task}</span>
                  <span className="sub mono">
                    {p.id} · for {p.consumer}
                  </span>
                </>
              ),
            },
            { head: "Files", cell: (p) => p.files, align: "right" },
            { head: <Term k="tokens">Tokens</Term>, cell: (p) => fmt(p.tokens), align: "right" },
            { head: "Built", cell: (p) => p.createdAt },
            { head: "State", cell: (p) => <Chip tone={p.stale === null ? "idle" : p.stale ? "warn" : "ok"}>{state(p)}</Chip> },
          ]}
        />
        )}
      </Panel>
    </>
  );
}
