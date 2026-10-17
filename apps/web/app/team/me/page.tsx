import Link from "next/link";
import { SectionTabs } from "@/components/SectionNav";
import { Chip, EmptyState, PageHead, Panel, Stat, Table, fmt } from "@/components/ui";
import { getMeView, type MeProject } from "@/lib/team-activity-view";

export const metadata = { title: "Me" };

const LEDE = "Who Brain sees when you use this dashboard: your team role, your project roles and repositories, and what the key you signed in with may do.";

export default async function Me() {
  const me = await getMeView();
  const head = (
    <>
      <PageHead title="Me" lede={LEDE} />
      <SectionTabs section="settings" active="/team/me" />
    </>
  );
  if (me.kind !== "ok") {
    return (
      <>
        {head}
        <Panel id="me-unavailable" title="Unavailable">
          <EmptyState title="Brain didn’t answer" body="Brain didn’t return your access just now, or this server predates /team/me. Reload in a moment." />
        </Panel>
      </>
    );
  }

  const role = me.teamState === "solo" ? "Owner" : me.teamAdmin ? "Team admin" : "Team member";
  return (
    <>
      {head}
      <div className="stats">
        <Stat label="Signed in as" value={me.principal.name} note={`${me.principal.kind || "principal"} #${me.principal.id}`} />
        <Stat label="Team" value={me.teamState === "solo" ? "Solo" : "Team"} note={role} />
        <Stat label="Projects" value={me.allRepositories ? "All" : fmt(me.projects.length)} note={me.allRepositories ? "every repository" : "with a role"} />
      </div>

      <Panel id="me-access" title="Your access" spine={me.allRepositories ? "ok" : "info"}>
        <div className="signin__state">
          <Chip tone={me.allRepositories ? "ok" : "info"}>{role}</Chip>
          <p>{me.accessReason || "Brain checks your access on every request."}</p>
          {me.canReadActivity && <Link className="btn btn--neutral" href="/team/activity">Open team activity</Link>}
        </div>
      </Panel>

      <Panel id="me-projects" title="Projects and roles" desc="Your live project roles. Effective permissions are the role’s permissions that the project allows." flush>
        {me.projects.length ? (
          <Table<MeProject>
            rows={me.projects}
            rowKey={(p) => p.id}
            caption="Your project roles"
            columns={[
              { head: "Project", cell: (p) => <>{p.name}{p.disabled && <> <Chip tone="idle">disabled</Chip></>}</> },
              { head: "Role", cell: (p) => p.role },
              { head: "Effective permissions", cell: (p) => (p.effectivePermissions.length ? p.effectivePermissions.join(", ") : "none") },
              { head: "Repositories", cell: (p) => (p.repositories.length ? p.repositories.map((r) => r.name || r.path).join(", ") : "none attached") },
            ]}
          />
        ) : (
          <EmptyState
            title={me.allRepositories ? "No project roles needed" : "No projects yet"}
            body={me.allRepositories ? "You can read and change every repository without a project role." : "Ask a team admin to give you a role in a project."}
          />
        )}
      </Panel>

      <Panel id="me-scopes" title="Key permissions" desc="Scopes of the key this dashboard uses for you. A write permission includes the matching read.">
        <div className="btn-row">
          {me.effectiveScopes.length ? me.effectiveScopes.map((s) => <Chip key={s} tone={s === "*" ? "ok" : "idle"} dot={false}>{s === "*" ? "all permissions" : s}</Chip>) : <span className="field__hint">This key has no scopes.</span>}
        </div>
      </Panel>
    </>
  );
}
