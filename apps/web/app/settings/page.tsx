import { adminOnlyPage } from "@/components/AdminOnly";
import { Settings } from "@/components/settings/Settings";
import { PageHead } from "@/components/ui";
import { SectionTabs } from "@/components/SectionNav";
import { getSettingsView } from "@/lib/settings-view";

export const metadata = { title: "Settings" };

export default async function SettingsPage() {
  const gate = await adminOnlyPage("Settings");
  if (gate) return gate;
  const view = await getSettingsView();
  return (
    <>
      <PageHead
        title="Settings"
        lede="Alerts, scheduled jobs, search and indexing. Saving updates the API immediately. Background workers use the saved settings after they restart; a save does not restart workers."
      />
      <SectionTabs section="settings" active="/settings" />
      {view.mode === "down" ? (
        <div className="callout tone-warn">
          <b>Brain isn’t answering.</b>
          <span>Start the server (docker compose up -d on the Brain machine), then reload this page.</span>
        </div>
      ) : (
        <>
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
