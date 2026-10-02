import type { CSSProperties } from "react";
import { CopyCommand } from "@/components/CopyCommand";
import { Term } from "@/components/Term";
import { EmptyState, Meter, PageHead } from "@/components/ui";
import { getSetupSteps } from "@/lib/data";

export const metadata = { title: "Get started" };

export default async function Setup() {
  const steps = await getSetupSteps();
  const done = steps.filter((s) => s.done).length;
  const next = steps.find((s) => !s.done);
  return (
    <>
      <PageHead
        eyebrow="Get started"
        title={steps.length === 0 ? "Setup status unavailable" : next ? `${done} of ${steps.length} steps done` : "Brain is fully set up"}
        lede={
          <>
            A few steps from zero to <Term k="agent">AI agents</Term> that know your code. Copy each command and run it in a
            terminal on the machine where Brain is installed.
          </>
        }
      />
      {steps.length === 0 ? (
        <EmptyState title="No setup steps to show" body="Brain didn’t report its setup status. Check that the API is reachable, then reload this page." />
      ) : (
      <Meter
        meter={{
          label: "Setup progress",
          value: `${done}/${steps.length}`,
          parts: [
            { tone: "filled", count: done, text: `${done} done` },
            { tone: "excluded", count: steps.length - done, text: `${steps.length - done} to go` },
          ],
        }}
      />
      )}
      <ol className="steps">
        {steps.map((s, i) => {
          const state = s.done ? "done" : s === next ? "next" : "todo";
          return (
            <li key={s.id} className={`step step--${state}`} style={{ "--i": i } as CSSProperties}>
              <span className="step__badge" aria-label={s.done ? "Done" : `Step ${i + 1}`}>
                {s.done ? (
                  <svg
                    className="step__check"
                    width={18}
                    height={18}
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={2.2}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    aria-hidden="true"
                  >
                    <polyline points="4 12.5 9.5 18 20 6" pathLength={1} />
                  </svg>
                ) : (
                  i + 1
                )}
              </span>
              <div>
                <p className="step__title">
                  {s.title}{" "}
                  {state === "next" && (
                    <span className="chip chip--info">
                      <span className="chip__dot" aria-hidden="true" />
                      Do this next
                    </span>
                  )}
                </p>
                <p className="step__detail">{s.detail}</p>
                {s.command && <CopyCommand command={s.command} />}
              </div>
            </li>
          );
        })}
      </ol>
    </>
  );
}
