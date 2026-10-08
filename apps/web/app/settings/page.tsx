import { Settings } from "@/components/settings/Settings";
import { Tip } from "@/components/Tip";
import { PageHead } from "@/components/ui";
import { SectionNav } from "@/components/SectionNav";
import { getSettingsView } from "@/lib/settings-view";

export const metadata = { title: "Settings" };

export default async function SettingsPage() {
  const view = await getSettingsView();
  return (
    <>
      <SectionNav label="Settings views" items={[{ href: "/settings", label: "Settings", active: true }, { href: "/admin", label: "Access" }, { href: "/mcp", label: "Agent tools" }]} />
      <PageHead
        eyebrow="Settings"
        title="Settings"
        lede={
          <>
            Alerts, automation, search and indexing.
            <Tip>Saved to the server’s .env and applied to the API at once. Background workers pick changes up after a restart.</Tip>
          </>
        }
      />
      {view.mode === "down" ? (
        <div className="callout tone-warn">
          <b>Brain isn’t answering.</b>
          <span>Start the server (docker compose up -d on the Brain machine), then reload this page.</span>
        </div>
      ) : (
        <>
          {view.mode === "demo" && (
            <div className="callout tone-info">
              <b>Demo data.</b>
              <span>Connect the dashboard to a Brain server (BRAIN_API_URL) to make these live.</span>
            </div>
          )}
          {view.mode === "legacy" && (
            <div className="callout tone-warn">
              <b>This server{view.version ? ` (${view.version})` : ""} can’t edit settings from here yet.</b>
              <span>Status is live; editing turns on after the server is updated.</span>
            </div>
          )}
          <Settings view={view} />
        </>
      )}
    </>
  );
}
