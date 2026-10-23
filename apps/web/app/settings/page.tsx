import { RetryRead } from "@/components/RetryRead";
import { Suspense } from "react";
import { adminOnlyPage } from "@/components/AdminOnly";
import { Settings } from "@/components/settings/Settings";
import { PageHead } from "@/components/ui";
import { SectionTabs } from "@/components/SectionNav";
import { getSettingsView } from "@/lib/settings-view";

export const metadata = { title: "Settings" };

export default async function SettingsPage() {
  const gate = await adminOnlyPage("Settings");
  if (gate) return gate;
  return <>
      <PageHead
        title="Settings"
        lede="Alerts, scheduled jobs, search and indexing. Saving updates the API immediately. Background workers use the saved settings after they restart; a save does not restart workers."
      />
      <SectionTabs section="settings" active="/settings" />
    <Suspense fallback={<p role="status" aria-live="polite">Loading measurements…</p>}><SettingsPageData  /></Suspense>
  </>;
}

async function SettingsPageData() {
  const view = await getSettingsView();
  return (
    <>

      {view.mode === "down" ? (
        <div className="callout tone-warn">
          <b>Settings measurements unavailable</b>
          <span>The settings and health requests failed. Retry, then check API connectivity and logs; this does not establish that the server stopped.</span><RetryRead />
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
