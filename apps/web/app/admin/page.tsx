import { Term } from "@/components/Term";
import { Chip, EmptyState, PageHead, Panel, Table } from "@/components/ui";
import { getPrincipals } from "@/lib/data";
import type { Principal } from "@/lib/types";

export const metadata = { title: "Access" };

const kindWords: Record<Principal["kind"], string> = { human: "person", agent: "AI agent", service: "integration" };

export default async function Admin() {
  const principals = await getPrincipals();
  return (
    <>
      <PageHead
        eyebrow="Access"
        title="Who and what can use Brain"
        lede={
          <>
            Every person, agent and integration has its own <Term k="principal">identity</Term> with keys you can revoke. Each
            one only gets the <Term k="scope">permissions</Term> it needs.
          </>
        }
      />
      <Panel id="principals" title={`${principals.length} identit${principals.length === 1 ? "y" : "ies"}`} flush>
        {principals.length === 0 ? (
          <EmptyState title="No identities to show" body="Brain didn’t list any active identities. Listing them needs an admin key." />
        ) : (
        <Table
          caption="Identities"
          rows={principals}
          rowKey={(p) => p.id}
          columns={[
            {
              head: "Name",
              cell: (p) => (
                <>
                  <span className="mono">{p.name}</span>
                  <span className="sub">{kindWords[p.kind]}</span>
                </>
              ),
            },
            {
              head: "Allowed to",
              cell: (p) => (
                <span className="chips">
                  {p.scopes.map((s) => (
                    <Chip key={s} tone={s === "*" ? "warn" : "idle"} dot={false}>
                      {s === "*" ? "everything" : s}
                    </Chip>
                  ))}
                </span>
              ),
            },
            { head: "Keys", cell: (p) => p.credentials, align: "right" },
            { head: "Last seen", cell: (p) => p.lastSeen, align: "right" },
          ]}
        />
        )}
      </Panel>
    </>
  );
}
