"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { Tone } from "@/lib/types";
import type { ActionResult, ActionsMode, SignInMode } from "@/lib/actions/types";
import { startBenchmark, startHealthCheck, startInsights, startReindex, startSelfDiagnosis } from "@/lib/actions/jobs";
import { verifyProviders } from "@/lib/actions/setup";
import { activeSection, adminOnly, allNavItems, locate, navGroups, withProject } from "@/lib/nav";
import { ActionsProvider, LOCK_REASON, Toaster, dismissToast, report, toast } from "./act";
import { Ambient } from "./Ambient";
import { BrandMark, Icon, type IconName } from "./Icon";
import { RestrictedMemberProvider, useRestrictedMember } from "./MemberScope";
import { RailPulse } from "./RailPulse";

export interface ShellRepo {
  slug: string;
  name: string;
  path: string;
  tone: Tone;
  status: string;
}

const isActive = (href: string, path: string) => (href === "/" ? path === "/" : path.startsWith(href));

/** Team state: who this browser is signed in as (names only; the key never reaches the client). */
export interface ShellMember {
  name: string;
  admin: boolean;
}

export function Shell({ repos, demo, mode, access, member = null, children }: { repos: ShellRepo[]; demo: boolean; mode: ActionsMode; access: SignInMode; member?: ShellMember | null; children: ReactNode }) {
  const path = usePathname();
  const params = useSearchParams();
  const page = locate(path)?.item;
  // A member who is not a team admin may only read their projects: the pages
  // that list rows they can read keep "All projects"; the dashboard, which
  // Brain only totals for an admin, opens on their first project.
  const restricted = access === "team" && member !== null && !member.admin;
  const allowsAllRepos = (restricted ? ["projects", "packs", "memory"] : ["command", "projects", "insights", "packs", "memory", "runs", "logs"]).includes(page?.key ?? "");
  const repoSlug = params.get("repo") ?? (page?.repoScoped && !allowsAllRepos ? repos[0]?.slug : undefined);
  const repo = repos.find((r) => r.slug === repoSlug || r.name.toLowerCase() === repoSlug?.toLowerCase());
  const [drawer, setDrawer] = useState(false);
  // Icons-only rail, remembered per browser. Medium screens get it from CSS.
  const [slim, setSlim] = useState(false);
  useEffect(() => {
    try {
      setSlim(localStorage.getItem("brain.rail") === "slim");
    } catch {}
  }, []);
  const toggleSlim = () =>
    setSlim((v) => {
      try {
        localStorage.setItem("brain.rail", v ? "full" : "slim");
      } catch {}
      return !v;
    });
  const [palette, setPalette] = useState(false);
  const [repoMenu, setRepoMenu] = useState(false);
  const [mobile, setMobile] = useState(false);
  const rail = useRef<HTMLElement>(null);
  const menuButton = useRef<HTMLButtonElement>(null);
  const paletteOpener = useRef<HTMLElement | null>(null);
  const openPalette = useCallback(() => {
    paletteOpener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setPalette(true);
  }, []);
  const closePalette = useCallback(() => {
    setPalette(false);
    requestAnimationFrame(() => paletteOpener.current?.isConnected && paletteOpener.current.focus());
  }, []);
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 560px)");
    const update = () => { setMobile(mq.matches); if (!mq.matches) setDrawer(false); };
    update(); mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    if (!drawer || !mobile) return;
    const el = rail.current;
    const focusable = () => Array.from(el?.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), [tabindex="0"]') ?? [])
      .filter((node) => node.getClientRects().length && getComputedStyle(node).visibility !== "hidden");
    focusable()[0]?.focus();
    const trap = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const nodes = focusable(); const first = nodes[0]; const last = nodes.at(-1);
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
    };
    el?.addEventListener("keydown", trap);
    return () => { el?.removeEventListener("keydown", trap); menuButton.current?.focus(); };
  }, [drawer, mobile]);

  const withRepo = useCallback(
    (href: string, slug = repo?.slug) => {
      return withProject(href, slug);
    },
    [repo?.slug, repos],
  );

  useEffect(() => {
    setDrawer(false);
    setRepoMenu(false);
  }, [path, repoSlug]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (palette) closePalette(); else openPalette();
      } else if (e.key === "Escape") {
        if (palette) closePalette();
        setDrawer(false);
        setRepoMenu(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [palette, openPalette, closePalette]);

  const where = locate(path);
  const current = where?.item;
  const scoped = Boolean(current?.repoScoped) && repos.length > 0;

  return (
    <RestrictedMemberProvider value={restricted}>
    <ActionsProvider mode={mode}>
    <div className={`shell${drawer ? " shell--drawer" : ""}${slim ? " shell--slim" : ""}`}>
      <Ambient />
      <a className="skip" href="#main">
        Skip to content
      </a>
      <aside ref={rail} className="rail" aria-label="Main navigation" inert={mobile && !drawer} aria-hidden={mobile && !drawer ? true : undefined}>
        <Link className="rail__brand" href={withRepo("/")} title="Project Brain — dashboard">
          <BrandMark size={30} />
          <span className="rail__word">
            Project <b>Brain</b>
            <small>memory for your AI agents</small>
          </span>
        </Link>
        <RailNav path={path} withRepo={withRepo} slim={slim} />
        {mobile && drawer && <button type="button" className="btn btn--neutral rail__close" onClick={() => setDrawer(false)}>Close menu</button>}
        <div className="rail__foot">
          {demo && process.env.NEXT_PUBLIC_DEMO_SITE === "1" && (
          <p className="rail__note">
            <a href="https://github.com/Perlitten/project-brain-public#readme">
              Read-only example → install your own
            </a>
          </p>
        )}
        {demo ? (
            <p className="rail__note">
              <span className="chip chip--info">
                <span className="chip__dot" aria-hidden="true" />
                Demo data
              </span>
              Numbers are examples until the API is connected.
            </p>
          ) : (
            <RailPulse withRepo={withRepo} repoSlug={allowsAllRepos && !repoSlug ? "" : repoSlug} />
          )}
          <Link className="rail__help" href={withRepo("/help")} aria-current={path === "/help" ? "page" : undefined} title="Help — about this server, common problems and the glossary">
            <Icon name="help" size={16} />
            <span className="rail__help-text">Help</span>
          </Link>
          <button
            type="button"
            className="rail__toggle"
            onClick={toggleSlim}
            aria-pressed={slim}
            title={slim ? "Show the menu with names" : "Collapse the menu to icons"}
          >
            <Icon name="sidebar" size={16} />
            <span className="rail__toggle-text">Collapse menu</span>
          </button>
        </div>
      </aside>
      <button className="scrim" type="button" aria-label="Close menu" aria-hidden={!drawer} tabIndex={-1} onClick={() => setDrawer(false)} />

      <header className="mast" inert={mobile && drawer}>
        <button ref={menuButton} className="mast__menu btn btn--neutral btn--sm" type="button" aria-expanded={drawer} onClick={() => setDrawer(true)}>
          <Icon name="menu" size={16} />
          Menu
        </button>
        <p className="mast__where crumb">
          {current && <Icon name={current.icon} />}
          {where && (
            <>
              <span className="crumb__group">{where.group}</span>
              <span className="crumb__sep" aria-hidden="true">/</span>
            </>
          )}
          <span className="crumb__page">{current?.label ?? "Project Brain"}</span>
        </p>
        <div className="mast__tools">
          {scoped && (
          <div className="repo">
            <button
              type="button"
              className="repo__btn"
              aria-expanded={repoMenu}
              onClick={() => setRepoMenu((o) => !o)}
            >
              <span className={`dot tone-${repo?.tone}`} aria-hidden="true" />
              <span className="repo__label">
                <small>Repository</small>
                {repo?.name ?? (allowsAllRepos && !repoSlug ? "All repositories" : "Repository not found")}
              </span>
              <Icon name="arrow" size={14} className="repo__caret" />
            </button>
            {repoMenu && (
              <ul className="repo__menu pop">
                {allowsAllRepos && <li><Link href={withRepo(`${path}${params.get("period") ? `?period=${params.get("period")}` : ""}`, "")} className={!repoSlug ? "is-on" : ""} aria-current={!repoSlug ? "true" : undefined}>All projects</Link></li>}
                {repos.map((r, i) => (
                  <li key={r.slug} style={{ "--i": i } as CSSProperties}>
                    <Link href={withRepo(`${path}${params.get("period") ? `?period=${params.get("period")}` : ""}`, r.slug)} className={r.slug === repo?.slug ? "is-on" : ""} aria-current={r.slug === repo?.slug ? "true" : undefined}>
                      <span className={`dot tone-${r.tone}`} aria-hidden="true" />
                      <span className="repo__name">{r.name}</span>
                      <span className="repo__status">{r.status}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </div>
          )}
          {access === "open" && (
            <Link className="access-chip chip chip--warn" href="/admin#sign-in" title="Anyone who opens this address gets in. Set a login and password.">
              <span className="chip__dot" aria-hidden="true" />
              <span>No password<span className="access-chip__more"> · set one</span></span>
            </Link>
          )}
          <button type="button" className="search-btn" aria-label="Go to a screen or run a command" onClick={openPalette}>
            <Icon name="search" size={16} />
            <span className="search-btn__text">Go or run…</span>
            <kbd>Ctrl K</kbd>
          </button>
          {access === "team" && member && (
            <Link className="access-chip chip chip--ok" href="/team" title={`Signed in as ${member.name}${member.admin ? " · team admin" : ""}. Brain checks this member's key on every request.`}>
              <span className="chip__dot" aria-hidden="true" />
              <span>{member.name}<span className="access-chip__more">{member.admin ? " · admin" : " · member"}</span></span>
            </Link>
          )}
          {(access === "password" || access === "env" || access === "team") && (
            <a className="btn btn--ghost btn--sm" href="/__logout" title="Sign out of this browser">
              Sign out
            </a>
          )}
        </div>
      </header>

      <main id="main" className="main" inert={mobile && drawer}>
        {demo && (
          <aside className="demo-notice" aria-label="Demo mode">
            <div>
              <strong>Demo data · synthetic examples</strong>
              <p>Read-only preview. No repository or model is connected; the numbers and project knowledge are examples.</p>
            </div>
            <a className="link" href="https://github.com/Perlitten/project-brain-public#readme">Install your own →</a>
          </aside>

        )}
        {children}
      </main>

      {palette && (
        <Palette
          onClose={closePalette}
          repos={repos}
          withRepo={withRepo}
          path={path}
          mode={mode}
          scoped={scoped}
          repoPath={repo?.path}
        />
      )}
      <Toaster />
    </div>
    </ActionsProvider>
    </RestrictedMemberProvider>
  );
}

// The active marker is one element that travels between items, so a route
// change reads as movement from where you were to where you are.
function RailNav({ path, withRepo, slim }: { path: string; withRepo: (href: string) => string; slim: boolean }) {
  const restricted = useRestrictedMember();
  const ref = useRef<HTMLElement>(null);
  const [mark, setMark] = useState<{ y: number; h: number } | null>(null);
  const [ready, setReady] = useState(false);

  useLayoutEffect(() => {
    const place = () => {
      const el = ref.current?.querySelector<HTMLElement>("[aria-current='page']");
      setMark(el ? { y: el.offsetTop, h: el.offsetHeight } : null);
    };
    place();
    // The rail changes shape when it collapses (by hand or at medium widths).
    const ro = new ResizeObserver(place);
    if (ref.current) ro.observe(ref.current);
    return () => ro.disconnect();
  }, [path, slim]);
  useEffect(() => {
    const t = requestAnimationFrame(() => setReady(true));
    return () => cancelAnimationFrame(t);
  }, []);

  return (
    <nav className={`nav${ready ? " nav--ready" : ""}`} ref={ref}>
      {mark && <span className="nav__mark" style={{ "--y": `${mark.y}px`, "--h": `${mark.h}px` } as CSSProperties} aria-hidden="true" />}
      {navGroups.map((g) => (
        <div className="nav__group" key={g.label}>
          <p className="nav__label">{g.label}</p>
          {g.items.map((item) => {
            // Settings opens on the Team page, the one settings page a member has.
            if (restricted && adminOnly(item.href) && item.key !== "settings") return null;
            const href = restricted && item.key === "settings" ? "/team" : item.href;
            const on = activeSection(path) === item.key;
            return (
              <Link
                key={item.key}
                href={withRepo(href)}
                className={`nav__item${on ? " is-on" : ""}`}
                aria-current={on ? "page" : undefined}
                title={`${item.label} — ${item.hint}`}
              >
                <Icon name={item.icon} />
                <span className="nav__text">{item.label}</span>
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}

interface Entry {
  id: string;
  label: string;
  hint: string;
  href?: string;
  /** A command instead of a destination. */
  run?: () => Promise<ActionResult>;
  icon?: IconName;
  tone?: Tone;
  group: string;
}

// Commands anyone can fire from Ctrl K. Each one is a real job or check; the
// toast follows it to the end.
const COMMANDS: { id: string; label: string; hint: string; icon: IconName; run: () => Promise<ActionResult> }[] = [
  { id: "reindex", label: "Re-index the repository", hint: "Read what changed since the last index", icon: "refresh", run: () => startReindex() },
  { id: "verify", label: "Verify model providers", hint: "Make one real call to the language model and embeddings", icon: "check", run: () => verifyProviders() },
  { id: "health", label: "Run a health check", hint: "Check the database, cache, graph and workers", icon: "activity", run: () => startHealthCheck() },
  { id: "insights", label: "Look for new findings", hint: "Scan the code structure for problems", icon: "trend", run: () => startInsights() },
  { id: "diagnose", label: "Run self-diagnosis", hint: "Brain checks its own pipeline end to end", icon: "zap", run: () => startSelfDiagnosis() },
  { id: "bench", label: "Benchmark search quality", hint: "Score how well search finds the right code", icon: "vectors", run: () => startBenchmark() },
];

function Palette({
  onClose,
  repos,
  withRepo,
  path,
  mode,
  scoped,
  repoPath,
}: {
  onClose: () => void;
  repos: ShellRepo[];
  withRepo: (href: string, slug?: string) => string;
  path: string;
  mode: ActionsMode;
  scoped: boolean;
  repoPath?: string;
}) {
  const restricted = useRestrictedMember();
  const router = useRouter();
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const listRef = useRef<HTMLUListElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  useLayoutEffect(() => {
    const d = dialog.current;
    if (d && !d.open) d.showModal();
    searchInput.current?.focus();
    return () => { if (d?.open) d.close(); };
  }, []);

  const entries = useMemo<Entry[]>(() => {
    const pages: Entry[] = allNavItems.filter((i) => !restricted || !adminOnly(i.href)).map((i) => ({
      id: i.key,
      label: i.label,
      hint: i.hint,
      href: withRepo(i.href),
      icon: i.icon,
      group: "Go to",
    }));
    // Server-wide jobs need a team admin; a member may still re-index a project they edit.
    const commands: Entry[] = COMMANDS.filter((c) => !restricted || c.id === "reindex").map((c) => ({ ...c,
      run: c.id === "reindex" ? () => repoPath ? startReindex({ repoPath }) : Promise.resolve({ ok: false, message: "Select an existing repository before re-indexing." }) : c.run,
      hint: c.id === "reindex" ? `Read changes in ${repoPath ?? "a repository — select one first"}` : `${c.hint} · Brain service / default repository`,
      id: `run-${c.id}`, group: "Run" }));
    const repoEntries: Entry[] = repos.map((r) => ({
      id: `repo-${r.slug}`,
      label: `Switch to ${r.name}`,
      hint: r.status,
      href: withRepo(scoped ? path : "/", r.slug),
      tone: r.tone,
      group: "Repository",
    }));
    const needle = q.trim().toLowerCase();
    return [...pages, ...commands, ...repoEntries].filter(
      (e) => !needle || e.label.toLowerCase().includes(needle) || e.hint.toLowerCase().includes(needle),
    );
  }, [q, repos, withRepo, path, scoped, repoPath, restricted]);

  useEffect(() => setSel(0), [q]);
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-i='${sel}']`)?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  const go = async (e?: Entry) => {
    if (!e) return;
    onClose();
    if (e.href) return router.push(e.href);
    if (!e.run) return;
    if (mode !== "on") return toast({ tone: "warn", title: e.label, message: LOCK_REASON[mode] });
    const pending = toast({ tone: "info", title: e.label, message: "Starting…" });
    let res: ActionResult;
    try {
      res = await e.run();
    } catch {
      res = { ok: false, message: "The dashboard couldn’t reach its server. Reload and try again." };
    }
    dismissToast(pending);
    report(res, e.label);
    if (res.ok) router.refresh();
  };

  return (
    <dialog ref={dialog} className="palette" aria-label="Jump to a screen" onCancel={(e) => { e.preventDefault(); onClose(); }}
      onKeyDown={(e) => {
        if (e.key !== "Tab") return;
        const nodes = Array.from(dialog.current?.querySelectorAll<HTMLElement>('input, button:not([disabled]):not([tabindex="-1"]), a[href]') ?? []);
        const first = nodes[0]; const last = nodes.at(-1);
        if (!nodes.length) { e.preventDefault(); dialog.current?.focus(); }
        else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
      }}>
      <button className="palette__scrim" type="button" aria-label="Close" onClick={onClose} tabIndex={-1} />
      <div className="palette__box">
        <div className="palette__field">
          <Icon name="search" size={18} />
          <input
            ref={searchInput}
            autoFocus
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Go to a screen or run a command — try “index” or “verify”"
            aria-label="Search screens, commands and repositories"
            aria-controls="palette-list"
            aria-activedescendant={entries[sel] ? `pal-${entries[sel].id}` : undefined}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setSel((s) => Math.max(0, Math.min(s + 1, entries.length - 1)));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setSel((s) => Math.max(s - 1, 0));
              } else if (e.key === "Enter") {
                go(entries[sel]);
              }
            }}
          />
          <kbd>Esc</kbd>
        </div>
        <ul className="palette__list" id="palette-list" role="listbox" ref={listRef}>
          {entries.length === 0 && <li className="palette__empty">Nothing matches “{q}”. Try a simpler word.</li>}
          {entries.map((e, i) => (
            <li
              key={e.id}
              id={`pal-${e.id}`}
              role="option"
              aria-selected={i === sel}
              data-i={i}
              className={`palette__row${i === sel ? " is-on" : ""}${e.run ? " palette__row--run" : ""}`}
              aria-disabled={e.run && mode !== "on" ? true : undefined}
              style={{ "--i": Math.min(i, 12) } as CSSProperties}
              onMouseMove={() => i !== sel && setSel(i)}
              onClick={() => go(e)}
            >
              {e.icon ? (
                <Icon name={e.icon} />
              ) : (
                <span className={`dot tone-${e.tone}`} aria-hidden="true" />
              )}
              <span className="palette__text">
                <span className="palette__label">{e.label}</span>
                <span className="palette__hint">{e.hint}</span>
              </span>
              <span className="palette__group">{e.group}</span>
            </li>
          ))}
        </ul>
        <p className="palette__foot">
          <span>
            <kbd>↑</kbd> <kbd>↓</kbd> choose
          </span>
          <span>
            <kbd>Enter</kbd> open or run
          </span>
        </p>
      </div>
    </dialog>
  );
}
