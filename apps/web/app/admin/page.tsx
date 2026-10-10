import { IdentityList, NewIdentityForm } from "@/components/access/Access";
import { Term } from "@/components/Term";
import { EmptyState, PageHead, Panel, Stat, fmt } from "@/components/ui";
import { SectionTabs } from "@/components/SectionNav";
import { getAccessView } from "@/lib/data";
import { getAgentTarget } from "@/lib/setup-view";

export const metadata = { title: "Access" };

export default async function Admin() {
  const [view, agent] = await Promise.all([getAccessView(), getAgentTarget()]);
  const { summary: s, identities } = view;
  const target = { apiUrl: agent.publicApiUrl, repoPath: agent.repoPath };
  return (
    <>
      <PageHead
        title="Access"
        lede={
          <>
            Every person, agent and integration has its own <Term k="principal">identity</Term> with keys you can revoke. Each
            one only gets the <Term k="scope">permissions</Term> it needs.
          </>
        }
      />
      <SectionTabs section="settings" active="/admin" />

      {view.readable && (
        <div className="stats">
          <Stat label="Identities" value={fmt(s.identities)} note={s.disabled ? `${s.disabled} disabled` : "all enabled"} />
          <Stat label="Active keys" value={fmt(s.activeKeys)} note="keys that work right now" />
          <div className={s.expiringSoon ? "tone-warn" : undefined}>
            <Stat label="Expiring soon" value={fmt(s.expiringSoon)} note="within 14 days" />
          </div>
          <Stat label="Never used" value={fmt(s.neverUsed)} note="active keys Brain hasn’t seen yet" />
        </div>
      )}

      <Panel id="new-identity" title="New identity" desc="Creates the identity and its first key. You’ll see the key once, right after.">
        <NewIdentityForm target={target} />
      </Panel>

      <Panel id="identities" title={`${identities.length} identit${identities.length === 1 ? "y" : "ies"}`} desc="Open one to see its keys, add a key or switch it off." flush>
        {!view.readable ? (
          <EmptyState
            title="Brain didn’t list any identities"
            body="Listing them needs a dashboard key with the principals:read permission — or Brain isn’t answering right now."
          />
        ) : identities.length === 0 ? (
          <EmptyState title="No identities yet" body="Create the first one above — an agent key is the usual start." />
        ) : (
          <IdentityList identities={identities} target={target} />
        )}
      </Panel>

      <div className="callout tone-info">
        <b>The master key</b>
        <span>
          Older integrations may still use <code>PROJECT_BRAIN_API_KEY</code>. It lives in the Brain server’s .env, can do everything and isn’t listed
          here. Give each integration its own key above, then change the master key on the server to retire it.
        </span>
      </div>
    </>
  );
}
