import { CopyCommand } from "@/components/CopyCommand";
import { Term } from "@/components/Term";
import { Tip } from "@/components/Tip";
import { SetupSteps } from "@/components/setup/SetupSteps";
import { PageHead, Panel } from "@/components/ui";
import { getClientConfigs } from "@/lib/data";
import { getSetupView } from "@/lib/setup-view";

export const metadata = { title: "Get started" };

export default async function Setup() {
  const [view, clients] = await Promise.all([getSetupView(), getClientConfigs()]);
  const steps = view.steps;
  const done = steps.filter((s) => s.done).length;
  const next = steps.find((s) => s.id === view.nextStep) ?? steps.find((s) => !s.done);
  return (
    <>
      <PageHead
        eyebrow="Get started"
        title={steps.length === 0 ? "Setup status unavailable" : next ? `${done} of ${steps.length} done` : "Brain is fully set up"}
        lede={
          <>
            Connect Brain to your code and your <Term k="agent">agents</Term>.
            <Tip>Each step opens the control that finishes it. Changes go straight to the Brain server.</Tip>
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
          <SetupSteps view={view} />
        </>
      )}
      {clients.length > 0 && <Panel id="client-configs" title="Agent configurations">
        {clients.map(client => <section key={client.name}>
          <h3>{client.name}</h3>
          <p>{client.where}</p>
          <CopyCommand command={client.config} />
          {client.cli && <CopyCommand command={client.cli} />}
        </section>)}
      </Panel>}
    </>
  );
}
