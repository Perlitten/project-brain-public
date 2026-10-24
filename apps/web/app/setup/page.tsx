import Link from "next/link";
import { adminOnlyPage } from "@/components/AdminOnly";
import { Term } from "@/components/Term";
import { SetupSteps } from "@/components/setup/SetupSteps";
import { STEP_TAB } from "@/components/setup/step-tabs";
import { HashView } from "@/components/settings/HashView";
import { ViewPane } from "@/components/PageTabs";
import { TabStrip, type TabStripItem } from "@/components/TabStrip";
import { PageHead, StatusLine } from "@/components/ui";
import { getClientConfigs, getLatestUncommittedChanges } from "@/lib/data";
import { uncommittedMessage } from "@/lib/repository-status";
import { stepTitle } from "@/lib/setup-copy";
import { getSetupView } from "@/lib/setup-view";
import { ReadProblem } from "@/components/ReadProblem";
import { getCapabilities } from "@/lib/capabilities";

export const metadata = { title: "Set up Brain" };

export default async function Setup({ searchParams }: { searchParams: Promise<{ from?: string | string[]; view?: string }> }) {
  const { from, view: requested } = await searchParams;
  const gate = await adminOnlyPage("Set up Brain");
  if (gate) return gate;
  const [view, clients, capabilities] = await Promise.all([getSetupView(), getClientConfigs(), getCapabilities()]);
  const steps = view.steps;
  const uncommitted = view.mode === "live" ? await getLatestUncommittedChanges(view.repoPath) : null;
  const done = steps.filter((s) => s.done).length;
  const next = steps.find((s) => s.id === view.nextStep) ?? steps.find((s) => !s.done);
  // A stepper: one tab per step, opening on the next step to do. Every tab
  // names its step in the address, since the default view is "the next one".
  const current = steps.find((s) => s.id === requested)?.id ?? next?.id ?? steps[0]?.id ?? "";
  const stepHref = Object.fromEntries(steps.map((s) => {
    const q = new URLSearchParams();
    if (from === "settings") q.set("from", "settings");
    q.set("view", s.id);
    return [s.id, `/setup?${q}`];
  }));
  const stepTabs: TabStripItem[] = steps.map((s, i) => {
    const [badge, tone] = s.done ? ["done", "ok"] as const : s.id === next?.id ? ["next", "info"] as const : s.status === "blocked" ? ["waiting", undefined] as const : ["to do", "warn"] as const;
    return { id: s.id, href: stepHref[s.id], label: <><span className="setup-tab__n">{i + 1}</span>{STEP_TAB[s.id] ?? stepTitle(s.id, s.title)}</>, badge, tone };
  });
  return (
    <>
      <PageHead
        title="Set up Brain"
        actions={from === "settings" ? <Link className="btn btn--neutral" href="/settings">← Back to Settings</Link> : undefined}
        status={steps.length === 0 ? { tone: "idle", text: "Setup status unavailable" } : next ? { tone: "info", text: <>{done} of {steps.length} steps done<small>Next: {stepTitle(next.id, next.title)}</small></> } : { tone: "ok", text: "Brain is fully set up" }}
        lede={
          <>
            Six steps that connect this Brain to your code and your <Term k="agent">agents</Term>. Changes go straight to the Brain server.
          </>
        }
      />
      {view.providersProblem && <ReadProblem problem={view.providersProblem} />}
      {view.mode === "down" ? (
        <ReadProblem problem={view.problem!} />
      ) : (
        <>
          {uncommitted !== null && uncommitted > 0 && <StatusLine tone="warn" text={<>{uncommittedMessage(uncommitted)}<small>Recorded at the latest index run for this repository, not a live checkout count. Commit the changes and reindex to make them available to agents.</small></>} />}
          {steps.length > 0 && <>
            <HashView views={Object.fromEntries(steps.map((s) => [s.id, s.id]))} current={current} first="" />
            <TabStrip label="Setup steps" current={current} items={stepTabs} />
            <ViewPane id={current}>
              <SetupSteps view={view} current={current} clients={clients} httpMcp={capabilities.supported.includes("mcp_http")} stepHref={stepHref} />
            </ViewPane>
          </>}
        </>
      )}
    </>
  );
}
