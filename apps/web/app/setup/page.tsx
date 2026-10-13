import { Term } from "@/components/Term";
import { SetupSteps } from "@/components/setup/SetupSteps";
import { PageHead } from "@/components/ui";
import { getClientConfigs } from "@/lib/data";
import { stepTitle } from "@/lib/setup-copy";
import { getSetupView } from "@/lib/setup-view";

export const metadata = { title: "Set up Brain" };

export default async function Setup() {
  const [view, clients] = await Promise.all([getSetupView(), getClientConfigs()]);
  const steps = view.steps;
  const done = steps.filter((s) => s.done).length;
  const next = steps.find((s) => s.id === view.nextStep) ?? steps.find((s) => !s.done);
  return (
    <>
      <PageHead
        title="Set up Brain"
        status={steps.length === 0 ? { tone: "idle", text: "Setup status unavailable" } : next ? { tone: "info", text: <>{done} of {steps.length} steps done<small>Next: {stepTitle(next.id, next.title)}</small></> } : { tone: "ok", text: "Brain is fully set up" }}
        lede={
          <>
            Brain indexes committed repository source and offers context to your coding <Term k="agent">agents</Term>.
            Reindex when source changes, and verify retrieved evidence in the code. These six steps configure it; changes go straight to the Brain server.
          </>
        }
      />
      {view.mode === "down" ? (
        <div className="callout tone-warn">
          <b>Brain isn’t answering.</b>
          <span>
            The dashboard can’t reach the Brain API, so it can’t show or change setup. Start the server (<code>docker compose up -d</code> on the Brain
            machine), then reload this page.
          </span>
        </div>
      ) : (
        <>
          {view.mode === "demo" && (
            <div className="callout tone-info">
              <b>Demo data.</b>
              <span>Connect the dashboard to a Brain server (BRAIN_API_URL) to make these steps live.</span>
            </div>
          )}
          <SetupSteps view={view} clients={clients} />
        </>
      )}
    </>
  );
}
