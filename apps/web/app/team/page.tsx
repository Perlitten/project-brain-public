import { getCapabilities } from "@/lib/capabilities";
import { capabilityGate } from "@/components/CapabilityGate";
import { CodeBlock } from "@/components/act";
import { Bootstrap } from "@/components/team/Bootstrap";
import { RetryRead } from "@/components/RetryRead";
import { InvitationTable, InviteForm } from "@/components/team/Invitations";
import { AddMemberForm, MemberTable } from "@/components/team/Team";
import { SectionTabs } from "@/components/SectionNav";
import { Chip, EmptyState, Fold, PageHead, Panel, Stat, fmt } from "@/components/ui";
import { signInMode } from "@/lib/sign-in-view";
import { getInvitationsView } from "@/lib/invitation-view";
import { currentMember } from "@/lib/team-session";
import { getTeamView } from "@/lib/team-view";

export const metadata = { title: "Team" };

const LEDE = "Everyone on the team signs in to this dashboard with their own key. Brain checks that key on every request, so revoking a member locks them out of the dashboard at once; revoke their key under Access to stop their agents too.";

export default async function Team() {
  const unsupported = await capabilityGate("team", "Team");
  if (unsupported) return unsupported;
  const [observedMode, member] = await Promise.all([signInMode(), currentMember()]);
  const mode = member ? "team" : observedMode;
  const head = (
    <>
      <PageHead title="Team" lede={LEDE} />
      <SectionTabs section="team" active="/team" />
    </>
  );

  if (mode === "unknown") return <>{head}<Panel id="team-status" title="Team status unavailable"><p>Brain did not return the sign-in state. Retry before changing team membership or server configuration.</p><RetryRead /></Panel></>;

  if (mode !== "team") {
    const bootstrap = (await getCapabilities()).supported.includes("team_bootstrap");
    return (
      <>
        {head}
        <Panel id="team-bootstrap" title="Enable team" desc="Prepare and save your own administrator key before changing how this dashboard signs you in.">{bootstrap ? <Bootstrap /> : <p>This server supports team membership but not dashboard bootstrap. Use the server setup below.</p>}</Panel>
        <Panel id="team-setup" title="Server setup and recovery" desc="Use this route when the dashboard credential cannot issue administrator scopes, or when you need to recover access.">
          <div className="form form--wide">
            <p className="field__hint">
              A team is set up on the Brain server, so the first admin is created by someone who can run commands there. Once the team has a member,
              the shared sign-in stops opening this dashboard and each member signs in with their own key.
            </p>
            <CodeBlock
              label="On the Brain server"
              code={`brain team init --name "My team"
brain team member add --principal alice --admin --mint-key`}
            />
            <p className="field__hint">
              Keep the printed key: it is the first admin’s dashboard sign-in. Admins then add or invite everyone else here, on the Team page.
            </p>
            <div className="callout tone-warn"><b>Keep a working way back in</b><span>Use the invitation route below to become the first admin in this browser. Save the member key before closing the invitation result. If you lose access, a server operator can run <code>brain team invite create --admin</code> to issue a recovery invitation for a new administrator. Changing the dashboard’s integration key does not recover a human account.</span></div>
            <p className="field__hint">
              Or bootstrap with an invitation: run <code>brain team invite create --admin --name you</code> on the server, then open{" "}
              <code>/invite</code> on this dashboard and paste the printed token. You become the first admin and are signed in with your own key.
            </p>
          </div>
        </Panel>
      </>
    );
  }

  if (!member?.admin) {
    return (
      <>
        {head}
        <Panel id="team-you" title="Your membership" spine="ok">
          <div className="signin__state">
            <Chip tone="ok">Team member</Chip>
            <b>{member ? <>Signed in as {member.name}</> : "Signed in"}</b>
            <p>Team admins add and revoke members and choose who can see each project. Ask one if you need access to another project.</p>
          </div>
        </Panel>
      </>
    );
  }

  const [view, invites] = await Promise.all([getTeamView(), getInvitationsView()]);
  const members = view.kind === "ok" ? view.members : [];
  const active = members.filter((m) => !m.revoked);
  return (
    <>
      {head}
      {view.kind === "ok" && (
        <div className="stats">
          <Stat label="Members" value={fmt(active.length)} note={members.length > active.length ? `${members.length - active.length} revoked` : "all active"} />
          <Stat label="Admins" value={fmt(active.filter((m) => m.admin).length)} note="can manage the team" />
          <Stat label="Projects" value={fmt(view.projects.filter((p) => !p.disabled).length)} note="roles are set per project" />
        </div>
      )}

      <Fold id="add-member" title="Add a member" desc="Creates the person and their dashboard key. You’ll see the key once, right after." cta="+ Add member">
        <AddMemberForm />
      </Fold>

      <Fold id="invite-member" title="Invite a member" desc="Creates a one-time link. They open it, become a member and pick up their own key; nobody else sees it." cta="+ Invite">
        <InviteForm projects={view.kind === "ok" ? view.projects : []} projectsAvailable={view.kind === "ok"} />
      </Fold>

      <Panel id="members" title={`${active.length} member${active.length === 1 ? "" : "s"}`} desc="Pick a role per project; revoking signs the member out on their next request." flush>
        {view.kind === "denied" ? (
          <EmptyState
            title="Brain didn’t list the team"
            body="Listing members needs a team admin whose key carries the team:admin permission. Ask another admin to issue you a new key."
          />
        ) : view.kind === "unavailable" ? (
          <EmptyState title="Brain isn’t answering" body="The member list couldn’t be read right now. Reload in a moment." />
        ) : members.length === 0 ? (
          <EmptyState title="No members yet" body="Add the first one above." />
        ) : (
          <MemberTable members={members} projects={view.projects} selfId={member.id} />
        )}
      </Panel>

      <Panel id="invitations" title="Invitations" desc="Pending, accepted and revoked invitations. Revoking an accepted one revokes the member it created too." flush>
        {invites.kind === "denied" ? (
          <EmptyState
            title="Brain didn’t list the invitations"
            body="Listing invitations needs a team admin whose key carries the team:admin permission. Ask another admin to issue you a new key."
          />
        ) : invites.kind === "unavailable" ? (
          <EmptyState title="Brain isn’t answering" body="The invitations couldn’t be read right now, so this isn’t an empty list. Reload in a moment." />
        ) : invites.invitations.length === 0 ? (
          <EmptyState title="No invitations yet" body="Invite someone above; their invitation shows up here until it’s accepted, expires or is revoked." />
        ) : (
          <InvitationTable invitations={invites.invitations} projects={view.kind === "ok" ? view.projects : []} selfId={member.id} />
        )}
      </Panel>

      <div className="callout tone-info">
        <b>Keys made elsewhere</b>
        <span>
          Keys from <code>brain team member add --mint-key</code> and from invitations carry the same permissions as keys issued here; Brain
          still limits each member to the projects and roles set above. A key minted before this release keeps the permissions it was issued
          with, so issue a new one if it can’t open the dashboard.
        </span>
      </div>
    </>
  );
}
