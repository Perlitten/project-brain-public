import { capabilityGate } from "@/components/CapabilityGate";
import { MyProposals, ReviewQueue } from "@/components/review/Review";
import { SectionTabs } from "@/components/SectionNav";
import { EmptyState, PageHead, Panel, Stat, fmt } from "@/components/ui";
import { getReviewView } from "@/lib/review-view";
import { signInMode } from "@/lib/sign-in-view";
import { currentMember } from "@/lib/team-session";

export const metadata = { title: "Review" };

const LEDE =
  "In a team, decisions and rules proposed by members wait here until a Reviewer on the project approves them. Agents only receive approved knowledge; a proposal never reaches a context pack, search result or rule check while it waits.";

export default async function Review() {
  const unsupported = await capabilityGate("knowledge_review", "Review");
  if (unsupported) return unsupported;
  const [mode, member] = await Promise.all([signInMode(), currentMember()]);
  const head = (
    <>
      <SectionTabs section="memory" active="/review" />
      <PageHead title="Review" lede={LEDE} />
    </>
  );

  if (mode !== "team") {
    return (
      <>
        {head}
        <Panel id="review-solo" title="Nothing to review" desc="This Brain has one owner.">
          <EmptyState
            title="The owner writes memory directly"
            body="Decisions and rules you record are approved knowledge at once, as before. Review starts when the Brain has a team: members then propose, and a Reviewer who is not the author approves."
          />
        </Panel>
      </>
    );
  }

  const view = await getReviewView(Boolean(member?.admin));
  if (view.kind === "unavailable") {
    return (
      <>
        {head}
        <Panel id="review-queue" title="Waiting for review">
          <EmptyState title="Proposals unavailable" body="Brain didn’t return its proposals just now. Reload in a moment." />
        </Panel>
      </>
    );
  }

  const selfId = member?.id ?? null;
  const reviewable = view.queue.filter((r) => r.authorId === null || r.authorId !== selfId).length;
  const pendingMine = view.mine.filter((r) => r.state === "pending").length;
  return (
    <>
      {head}
      <div className="stats">
        <Stat label="Waiting for you" value={fmt(reviewable)} note={view.queueScopes ? "proposals by other members" : "you don’t review any project"} />
        <Stat label="Your pending proposals" value={fmt(pendingMine)} note="another member decides them" />
      </div>
      <Panel
        id="review-queue"
        title="Waiting for review"
        desc="Approving publishes the proposal as approved knowledge and records you as the reviewer. Only a Reviewer on the project who did not write it can approve; system-wide proposals need a team admin."
        flush
      >
        {view.queue.length === 0 ? (
          <EmptyState
            title={view.queueScopes ? "Nothing waiting" : "No review queue"}
            body={
              view.queueScopes
                ? "No proposals are waiting in the projects you review."
                : "You aren’t a Reviewer on any project. Team admins choose project roles on the Team page."
            }
          />
        ) : (
          <ReviewQueue rows={view.queue} selfId={selfId} />
        )}
      </Panel>
      <Panel id="review-mine" title="Your proposals" desc="Proposals you or your agents made with your key, newest first." flush>
        {view.mine.length === 0 ? (
          <EmptyState
            title="No proposals yet"
            body="When you or your agent record a decision or rule in a team project, it is proposed here instead of written directly."
          />
        ) : (
          <MyProposals rows={view.mine} />
        )}
      </Panel>
    </>
  );
}
