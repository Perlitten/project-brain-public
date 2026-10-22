import { capabilityGate } from "@/components/CapabilityGate";
import Link from "next/link";
import { SectionTabs } from "@/components/SectionNav";
import { Chip, EmptyState, PageHead, Panel, Stat, Table, fmt } from "@/components/ui";
import { NotificationInbox, TelegramLinkForm } from "@/components/team/Notifications";
import { getMeView, type MeProject } from "@/lib/team-activity-view";
import { getNotifications, getTelegramLink } from "@/lib/team-notifications-view";

export const metadata = { title: "Me" };

const LEDE = "Who Brain sees when you use this dashboard: your team role, your project roles and repositories, and what the key you signed in with may do.";

export default async function Me() {
  const unsupported = await capabilityGate("team", "Me");
  if (unsupported) return unsupported;
  const [me, inbox, telegram] = await Promise.all([getMeView(), getNotifications(), getTelegramLink()]);
  const head = (
    <>
      <PageHead title="Me" lede={LEDE} />
      <SectionTabs section="team" active="/team/me" />
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

      {me.teamState === "team" && (
        <Panel
          id="me-notifications"
          title="Notifications"
          desc="Memory proposals that wait for your review. Only you see this inbox."
          spine={inbox.kind === "ok" && inbox.unread > 0 ? "info" : undefined}
          flush={inbox.kind === "ok" && inbox.rows.length > 0}
        >
          {inbox.kind !== "ok" ? (
            <EmptyState title="Notifications unavailable" body="Brain didn’t return your notifications just now, or this server predates the inbox. Reload in a moment." />
          ) : inbox.rows.length ? (
            <NotificationInbox rows={inbox.rows} unread={inbox.unread} />
          ) : (
            <EmptyState title="Nothing to review" body="When a proposal needs your review, it shows up here." />
          )}
        </Panel>
      )}

      {me.teamState === "team" && telegram.kind === "ok" && (
        <Panel id="me-telegram" title="Telegram notices" desc="Get a short Telegram message when a proposal waits for your review. Only you can link or unlink your chat.">
          {telegram.available || telegram.linked ? (
            <TelegramLinkForm link={telegram} />
          ) : (
            <EmptyState title="Not enabled on this Brain" body="An operator turns on per-member Telegram notices with TEAM_MEMBER_TELEGRAM_NOTIFY and a bot token. Your inbox above works either way." />
          )}
        </Panel>
      )}

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
