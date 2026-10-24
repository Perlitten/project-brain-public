import Link from "next/link";
import { SectionTabs } from "@/components/SectionNav";
import { MemoryBoard, type MemoryTab } from "@/components/atlas/MemoryBoard";
import { PageHead } from "@/components/ui";
import { getCapabilities } from "@/lib/capabilities";
import { getPagedDecisions, getPagedRules, getRepositoryFeed } from "@/lib/data";
import { getListQuery } from "@/lib/list-query";
import { getReviewView } from "@/lib/review-view";
import { signInMode } from "@/lib/sign-in-view";
import { currentMember } from "@/lib/team-session";

export const metadata = { title: "Memory" };

// The map draws this many records of each kind per page; the tabs still count every one.
const DRAWN = 100;

/** Proposals the viewer could review, counted the way /review counts them; 0 outside a team. */
async function waitingForReview(): Promise<number> {
  const [features, mode] = await Promise.all([getCapabilities(), signInMode()]);
  if (mode !== "team" || !features.supported.includes("knowledge_review")) return 0;
  const member = await currentMember();
  const view = await getReviewView(Boolean(member?.admin));
  if (view.kind !== "ok") return 0;
  const selfId = member?.id ?? null;
  return view.queue.filter((r) => r.authorId === null || r.authorId !== selfId).length;
}

const pickView = (view: string | undefined, status: string | undefined): MemoryTab =>
  status === "superseded" || view === "replaced" ? "replaced" : view === "rules" ? "rules" : view === "decisions" ? "decisions" : "current";

// Memory answers what agents are told besides code: the sentence counts it,
// the map shows where each decision and rule applies, the inspector edits one.
export default async function Memory({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const one = (k: string) => (typeof params[k] === "string" ? (params[k] as string) : undefined);
  const repo = one("repo");
  const { q, page } = getListQuery({ q: one("q") ?? "", page: Number(one("page")) || 1, size: DRAWN });
  // Web ids read "D-42"; Brain matches decisions on the bare number.
  const decisionQ = q.replace(/^D-(?=\d+$)/i, "");
  const [current, replaced, rules, waiting, feed] = await Promise.all([
    getPagedDecisions(repo, { size: DRAWN, page, q: decisionQ, status: "current" }),
    getPagedDecisions(repo, { size: DRAWN, page, q: decisionQ, status: "superseded" }),
    getPagedRules(repo, { size: DRAWN, page, q }),
    waitingForReview(),
    getRepositoryFeed(),
  ]);
  const scoped = repo ? feed.repositories.find((r) => r.slug === repo || r.name.toLowerCase() === repo.toLowerCase()) : undefined;
  const projects = (scoped ? [scoped] : feed.repositories).map((r) => ({ slug: r.slug, name: r.name, path: r.path }));
  // ?new=rule&title=…&description=… opens the record panel filled in (a finding turned into memory).
  const newKind = one("new");
  const prefill = newKind === "rule" || newKind === "decision" ? { kind: newKind as "rule" | "decision", title: one("title")?.slice(0, 500), description: one("description")?.slice(0, 10000) } : undefined;
  return (
    <>
      <SectionTabs section="memory" active="/memory" />
      <PageHead eyebrow={scoped ? scoped.name : "All projects"} title="Memory" />
      {waiting > 0 && (
        <p className="callout tone-info review-waiting" role="status">
          <span>
            <b>
              {waiting.toLocaleString("en")} proposal{waiting === 1 ? "" : "s"} waiting for review.
            </b>{" "}
            Agents don’t receive them until a Reviewer approves.
          </span>
          <Link className="btn btn--neutral" href="/review">
            Open Review
          </Link>
        </p>
      )}
      <MemoryBoard
        decisions={current.available === false ? [] : current.items}
        replaced={replaced.available === false ? [] : replaced.items}
        rules={rules.available === false ? [] : rules.items}
        projects={projects}
        scoped={Boolean(scoped)}
        tab={pickView(one("view"), one("status"))}
        query={{ repo, q, page }}
        size={DRAWN}
        totals={{ decisions: current.paging.total, rules: rules.paging.total, replaced: replaced.paging.total }}
        repoPath={scoped?.path}
        unavailable={{ decisions: current.available === false, rules: rules.available === false, replaced: replaced.available === false }}
        prefill={prefill}
      />
    </>
  );
}
