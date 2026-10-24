import { Suspense, type ReactNode } from "react";
import Link from "next/link";
import { RetryRead } from "@/components/RetryRead";
import { PageHead } from "@/components/ui";
import { TodayBoard, type TodayRegion } from "@/components/atlas/TodayBoard";
import { InboxAgents, InboxNeeds } from "@/components/inbox/Inbox";
import { PageTabs, ViewPane, pickTab, type TabTone } from "@/components/PageTabs";
import { getInbox } from "@/lib/inbox";
import { getCodeModules, getContextPacks, getCorpus, getRepositoryFeed } from "@/lib/data";
import { apiConfigured } from "@/lib/api";
import { scopeMemberToProject } from "@/lib/member-scope";
import { restrictedMember } from "@/lib/team-session";
import { canReindex, repositoryAction, repositoryStatus, repositorySummary, SOURCE_UNAVAILABLE_HELP } from "@/lib/repository-status";
import { moduleTiles, moduleTotals, todayVerdict, type Segment } from "@/lib/atlas";
import { RunButton } from "@/components/act";
import { startReindex } from "@/lib/actions/jobs";
import type { Tone } from "@/lib/types";

export const metadata = { title: "Dashboard" };

type Props = { searchParams: Promise<{ repo?: string; view?: string }> };
const VIEWS = ["needs", "agents", "freshness"] as const;
const severity: Record<Tone, number> = { bad: 3, warn: 2, info: 1, idle: 1, ok: 0 };
// The map draws at most this many projects; the sentence still counts all of them.
const MAP_PROJECTS = 8;
const PACKS = 12;
type Ranked = { repo: { behind: number | null }; tone: Tone };
const byAttention = (a: Ranked, b: Ranked) => severity[b.tone] - severity[a.tone] || (b.repo.behind ?? 0) - (a.repo.behind ?? 0);

// The home screen answers one question first, in a sentence: is the memory
// your agents get current, and if not, where. The map shows where in the code,
// the feed shows what agents actually got. Metrics live on their own screens.
export default function Dashboard(props: Props) {
  const today = new Date().toLocaleDateString("en-GB", { timeZone: "UTC", weekday: "long", day: "numeric", month: "long" });
  return <><PageHead eyebrow={today} title="Dashboard" /><Suspense fallback={<p role="status" aria-live="polite">Reading project memory…</p>}><DashboardData {...props} /></Suspense></>;
}

async function DashboardData({ searchParams }: Props) {
  const query = await searchParams;
  await scopeMemberToProject("/", query);
  const slug = query.repo;
  const feed = await getRepositoryFeed();
  const repositories = feed.repositories;
  const selected = slug ? repositories.find((r) => r.slug === slug || r.name.toLowerCase() === slug.toLowerCase()) : undefined;
  const projects = slug ? selected ? [selected] : [] : repositories;
  const [baseSnapshots, packs, member] = await Promise.all([
    Promise.all(projects.map(async (repo) => {
      const corpus = await getCorpus(repo.slug);
      return { repo, corpus, ...repositoryStatus(repo, corpus.uncommitted) };
    })),
    getContextPacks(PACKS),
    restrictedMember(),
  ]);
  // The verdict names the projects needing attention first. Draw those same
  // projects, worst first, so every mention can focus a real region even when
  // there are more repositories than the map's visual limit.
  const ranked = [...baseSnapshots].sort(byAttention);
  const snapshots = await Promise.all(ranked.map(async (s, i) => ({
    ...s,
    modules: i < MAP_PROJECTS ? await getCodeModules(s.repo.slug) : null,
  })));
  // Activity and Quality cover the whole server: only team admins get links to them.
  const admin = member === null;
  const attention = snapshots.filter((s) => s.tone !== "ok");
  const first = attention[0];
  const nextAction = first ? repositoryAction(first.repo) : null;
  const observations = new Map(snapshots.map((s) => [s.repo.id, s.corpus.uncommitted ?? null]));

  let segments: Segment[];
  let sub: ReactNode = null;
  let actions: ReactNode = null;
  if (!feed.available) {
    segments = ["Brain can't list your projects right now."];
    sub = `${feed.note}. Check the Brain connection and retry.`;
    actions = admin ? <Link className="btn btn--neutral" href="/setup">Check connection</Link> : null;
  } else if (slug && !selected) {
    segments = ["This project is not connected to Brain."];
    sub = "Nothing is shown for another project in its place.";
    actions = <Link className="btn btn--neutral" href="/projects">Choose a project</Link>;
  } else if (!projects.length) {
    segments = ["No projects are connected yet."];
    sub = "Brain has nothing to give agents until a repository is indexed.";
    actions = admin ? <Link className="btn btn--primary" href="/setup">Connect a project</Link> : null;
  } else {
    segments = todayVerdict(snapshots.map((s) => ({ slug: s.repo.slug, name: s.repo.name, behind: s.repo.behind, freshness: s.repo.freshness, ok: s.tone === "ok", status: s.status })));
    sub = <>{repositorySummary(projects, observations)}{first?.repo.freshness === "source_missing" ? `. ${SOURCE_UNAVAILABLE_HELP}` : "."}</>;
    // The fix is one click when Brain can read the source; the run history stays one link away.
    actions = first
      ? admin && canReindex(first.repo)
        ? <><RunButton variant="primary" action={async () => { "use server"; return startReindex({ repoPath: first.repo.path }); }} label={`Re-index ${first.repo.name}`} title={`Re-index ${first.repo.name}`} />{nextAction && <Link className="btn btn--neutral" href={nextAction.href}>Index runs</Link>}</>
        : nextAction ? <Link className="btn btn--neutral" href={nextAction.href}>{nextAction.label}</Link> : null
      : null;
  }

  const regions: TodayRegion[] = snapshots.filter((s) => s.modules !== null).map((s) => {
    const modules = s.modules ?? [];
    const tiles = moduleTiles(modules);
    const behind = s.repo.behind ?? 0;
    const note = s.repo.freshness === "source_missing" ? "source unavailable"
      : behind > 0 ? `${behind.toLocaleString("en-US")} behind`
      : s.tone === "ok" ? "current" : s.status.toLowerCase();
    return {
      key: s.repo.slug,
      name: s.repo.name,
      href: `/projects?repo=${encodeURIComponent(s.repo.slug)}`,
      note,
      noteTone: s.repo.freshness === "source_missing" || behind > 20 ? "bad" : s.tone === "ok" ? "ok" : "warn",
      weight: Math.min(3, Math.max(1, tiles.reduce((n, t) => n + t.span, 0) / 6)),
      tiles,
      totals: moduleTotals(modules),
      empty: s.corpus.available === false ? "Brain could not read what it holds for this project." : "Nothing indexed yet.",
    };
  });

  // The inbox first: what asks for a person across the Board, findings, tools,
  // keys and setup, then who is working. The freshness verdict and map follow.
  const inbox = await getInbox(admin, snapshots.map((s) => ({ repo: s.repo, tone: s.tone, status: s.status, available: s.corpus.available })));
  const unread = snapshots.some((s) => s.corpus.available === false) || !feed.available || !inbox.complete;
  const tab = pickTab(VIEWS, query.view);
  // An inbox Brain could not fully check is never an all-clear.
  const needsTone: TabTone = inbox.needs.some((n) => n.tone === "bad") ? "bad" : inbox.needs.length || !inbox.complete ? "warn" : "ok";
  const staleTone: TabTone = attention.some((s) => s.tone === "bad") ? "bad" : attention.length ? "warn" : "ok";
  return <>
    {unread && <div className="callout tone-warn" role="status"><b>Some measurements could not be read</b><span>Timeouts describe a failed request, not a stopped server. Retry, then check API logs if the problem persists.</span><RetryRead /></div>}
    <PageTabs
      label="Dashboard views"
      path="/"
      params={{ repo: slug }}
      current={tab}
      tabs={[
        { id: "needs", label: "Needs you", badge: inbox.needs.length, tone: needsTone },
        { id: "agents", label: "Agents working", badge: inbox.agents.length, tone: inbox.agents.length ? "info" : undefined },
        { id: "freshness", label: "Memory freshness", badge: attention.length ? `${attention.length} stale` : "current", tone: staleTone },
      ]}
    />
    <ViewPane id={tab} fill={tab !== "freshness"}>
      {tab === "needs" && <InboxNeeds needs={inbox.needs} complete={inbox.complete} unavailable={inbox.unavailable} />}
      {tab === "agents" && <InboxAgents agents={inbox.agents} />}
      {tab === "freshness" && <TodayBoard
        segments={segments}
        sub={sub}
        actions={actions}
        regions={regions}
        hidden={Math.max(0, snapshots.length - MAP_PROJECTS)}
        packs={packs}
        live={apiConfigured}
        footer={admin ? <>Background jobs live in <Link href="/activity">Activity</Link>, search quality in <Link href="/quality">Quality</Link>.</> : null}
      />}
    </ViewPane>
  </>;
}
