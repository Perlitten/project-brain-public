import Link from "next/link";
import { adminOnlyPage } from "@/components/AdminOnly";
import { IdentityList, NewIdentityForm } from "@/components/access/Access";
import { SignIn } from "@/components/access/SignIn";
import { Term } from "@/components/Term";
import { EmptyState, Fold, PageHead, Panel, Stat, fmt } from "@/components/ui";
import { SectionTabs } from "@/components/SectionNav";
import { getAccessView } from "@/lib/data";
import { getAgentTarget } from "@/lib/setup-view";
import { getSignInView } from "@/lib/sign-in-view";
import { currentMember } from "@/lib/team-session";
import { getCapabilities } from "@/lib/capabilities";

export const metadata = { title: "Access" };

export default async function Admin({ searchParams }: { searchParams: Promise<{ new?: string }> }) {
  const gate = await adminOnlyPage("Access");
  if (gate) return gate;
  const [view, agent, signIn, capabilities] = await Promise.all([getAccessView(), getAgentTarget(), getSignInView(), getCapabilities()]);
  const { summary: s, identities } = view;
  // Setup and Agent tools link here with ?new=agent: keys are created in this one place.
  const asked = (await searchParams).new === "agent";
  const target = { apiUrl: agent.publicApiUrl, repoPath: agent.repoPath, httpMcp: capabilities.supported.includes("mcp_http") };
  // In team state only a team admin is offered new identities; Brain still checks the call.
  const canCreate = signIn.mode !== "team" || (await currentMember())?.admin === true;
  return (
    <>
      <SectionTabs section="settings" active="/admin" />
      <PageHead
        title="Access"
        lede={
          <>
            Who can open this dashboard, and which agents and integrations can talk to Brain. Every person, agent and integration has its own <Term k="principal">identity</Term> with keys you can revoke. Each
            one only gets the <Term k="scope">permissions</Term> it needs.
          </>
        }
      />
      <p className="access-crosslink">Access is API identities and agent or service keys. People and their project roles are under <Link href="/team">Team</Link>.</p>

      <SignIn view={signIn} />

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

      {canCreate && (
        <Fold
          id="new-identity"
          title="New identity"
          desc="Creates the identity and its first key. You’ll see the key once, right after."
          cta="+ New identity"
          open={asked || (view.readable && identities.length === 0)}
        >
          <NewIdentityForm target={target} />
        </Fold>
      )}

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
          here. Check its recent requests under Activity before rotating it: existing clients stop working until their configuration is updated. Give each active integration its own key, verify it works, then change the master key on the server.
        </span>
      </div>
    </>
  );
}
