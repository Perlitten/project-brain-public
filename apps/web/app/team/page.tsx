import { CodeBlock } from "@/components/act";
import { AddMemberForm, MemberTable } from "@/components/team/Team";
import { SectionTabs } from "@/components/SectionNav";
import { Chip, EmptyState, Fold, PageHead, Panel, Stat, fmt } from "@/components/ui";
import { signInMode } from "@/lib/sign-in-view";
import { currentMember } from "@/lib/team-session";
import { getTeamView } from "@/lib/team-view";

export const metadata = { title: "Team" };

const LEDE = "Everyone on the team signs in to this dashboard with their own key. Brain checks that key on every request, so revoking a member locks them out at once.";

export default async function Team() {
  const [mode, member] = await Promise.all([signInMode(), currentMember()]);
  const head = (
    <>
      <PageHead title="Team" lede={LEDE} />
      <SectionTabs section="settings" active="/team" />
    </>
  );

  if (mode !== "team") {
    return (
      <>
        {head}
        <Panel id="team-setup" title="No team yet" desc="This Brain has one owner: the dashboard opens with the owner’s sign-in and the server key.">
          <div className="form form--wide">
            <p className="field__hint">
              A team is set up on the Brain server, so the first admin is created by someone who can run commands there. Once the team has a member,
              the shared sign-in stops opening this dashboard and each member signs in with their own key.
            </p>
            <CodeBlock
              label="On the Brain server"
              code={`brain team init --name "My team"
python scripts/mint_api_credential.py --name alice --kind human --scopes "*"
brain team member add --principal alice --admin`}
            />
            <p className="field__hint">
              Keep the printed key: it is the first admin’s dashboard sign-in. Admins then add everyone else here, on the Team page.
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

  const view = await getTeamView();
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

      <div className="callout tone-info">
        <b>Keys made elsewhere</b>
        <span>
          A key minted with the CLI only opens what its permissions allow. Keys issued here carry the dashboard’s read permissions plus the team
          permissions; Brain still limits each member to the projects and roles set above.
        </span>
      </div>
    </>
  );
}
