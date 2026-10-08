import { AgentConfigurations } from "@/components/setup/AgentConfigurations";
import { SectionNav } from "@/components/SectionNav";
import { Term } from "@/components/Term";
import { Tip } from "@/components/Tip";
import { SetupSteps } from "@/components/setup/SetupSteps";
import { PageHead, Panel } from "@/components/ui";
import { getClientConfigs } from "@/lib/data";
import { getSetupView } from "@/lib/setup-view";

export const metadata = { title: "Get started" };

export default async function Setup({ searchParams }: { searchParams: Promise<{ repo?: string }> }) {
  const { repo } = await searchParams;
  const [view, clients] = await Promise.all([getSetupView(), getClientConfigs()]);
  const steps = view.steps;
  const done = steps.filter((s) => s.done).length;
  const next = steps.find((s) => s.id === view.nextStep) ?? steps.find((s) => !s.done);
  return (
    <>
      <SectionNav label="Project views" items={[{ href: `/projects${repo ? `?repo=${repo}` : ""}`, label: "Projects" }, { href: `/indexing${repo ? `?repo=${repo}` : ""}`, label: "Indexing" }, { href: `/graph${repo ? `?repo=${repo}` : ""}`, label: "Code map" }, { href: `/setup${repo ? `?repo=${repo}` : ""}`, label: "Connection", active: true }]} />
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
      {clients.length > 0 && <Panel id="client-configs" title="Connect your agent" desc="Choose your client, then add Brain to its configuration.">
        <AgentConfigurations clients={clients} />
      </Panel>}
    </>
  );
}
