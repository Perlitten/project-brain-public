import { Settings } from "@/components/settings/Settings";
import { PageHead } from "@/components/ui";
import { SectionTabs } from "@/components/SectionNav";
import { getSettingsView } from "@/lib/settings-view";

export const metadata = { title: "Settings" };

export default async function SettingsPage() {
  const view = await getSettingsView();
  return (
    <>
      <PageHead
        title="Settings"
        lede="Alerts, scheduled jobs, search and indexing. Changes are saved on the Brain server and take effect at once; background workers pick them up after a restart."
      />
      <SectionTabs section="settings" active="/settings" />
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
