import Link from "next/link";
import { adminOnlyPage } from "@/components/AdminOnly";
import { Term } from "@/components/Term";
import { SetupSteps } from "@/components/setup/SetupSteps";
import { PageHead, StatusLine } from "@/components/ui";
import { getClientConfigs, getLatestUncommittedChanges } from "@/lib/data";
import { uncommittedMessage } from "@/lib/repository-status";
import { stepTitle } from "@/lib/setup-copy";
import { getSetupView } from "@/lib/setup-view";
import { ReadProblem } from "@/components/ReadProblem";

export const metadata = { title: "Set up Brain" };

export default async function Setup({ searchParams }: { searchParams: Promise<{ from?: string | string[] }> }) {
  const { from } = await searchParams;
  const gate = await adminOnlyPage("Set up Brain");
  if (gate) return gate;
  const [view, clients] = await Promise.all([getSetupView(), getClientConfigs()]);
  const steps = view.steps;
  const uncommitted = view.mode === "live" ? await getLatestUncommittedChanges(view.repoPath) : null;
  const done = steps.filter((s) => s.done).length;
  const next = steps.find((s) => s.id === view.nextStep) ?? steps.find((s) => !s.done);
  return (
    <>
      <PageHead
        title="Set up Brain"
        actions={from === "settings" ? <Link className="btn btn--neutral" href="/settings#models">← Back to Settings</Link> : undefined}
        status={steps.length === 0 ? { tone: "idle", text: "Setup status unavailable" } : next ? { tone: "info", text: <>{done} of {steps.length} steps done<small>Next: {stepTitle(next.id, next.title)}</small></> } : { tone: "ok", text: "Brain is fully set up" }}
        lede={
          <>
            Brain indexes committed repository source and offers context to your coding <Term k="agent">agents</Term>.
            Reindex when source changes, and verify retrieved evidence in the code. These six steps configure it; changes go straight to the Brain server.
          </>
        }
      />
      {view.providersProblem && <ReadProblem problem={view.providersProblem} />}
      {view.mode === "down" ? (
        <ReadProblem problem={view.problem!} />
      ) : (
        <>
          {uncommitted !== null && uncommitted > 0 && <StatusLine tone="warn" text={<>{uncommittedMessage(uncommitted)}<small>Recorded at the latest index run for this repository, not a live checkout count. Commit the changes and reindex to make them available to agents.</small></>} />}
          <SetupSteps view={view} clients={clients} />
        </>
      )}
    </>
  );
}
