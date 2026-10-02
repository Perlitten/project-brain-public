"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { Tone } from "@/lib/types";
import { allNavItems, navGroups } from "@/lib/nav";
import { BrandMark, Icon } from "./Icon";

export interface ShellRepo {
  slug: string;
  name: string;
  tone: Tone;
  status: string;
}

const isActive = (href: string, path: string) => (href === "/" ? path === "/" : path.startsWith(href));

export function Shell({ repos, demo, children }: { repos: ShellRepo[]; demo: boolean; children: ReactNode }) {
  const path = usePathname();
  const params = useSearchParams();
  const repoSlug = params.get("repo") ?? repos[0]?.slug;
  const repo = repos.find((r) => r.slug === repoSlug) ?? repos[0];
  const [drawer, setDrawer] = useState(false);
  const [palette, setPalette] = useState(false);
  const [repoMenu, setRepoMenu] = useState(false);

  const withRepo = useCallback(
    (href: string, slug = repo?.slug) => (slug && slug !== repos[0]?.slug ? `${href}?repo=${slug}` : href),
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
        setPalette((p) => !p);
      } else if (e.key === "Escape") {
        setPalette(false);
        setDrawer(false);
        setRepoMenu(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const current = allNavItems.find((i) => isActive(i.href, path));

  return (
    <div className={`shell${drawer ? " shell--drawer" : ""}`}>
      <a className="skip" href="#main">
        Skip to content
      </a>
      <aside className="rail" aria-label="Main navigation">
        <Link className="rail__brand" href={withRepo("/")}>
          <BrandMark size={26} />
          <span>
            Project Brain
            <small>memory for your AI agents</small>
          </span>
        </Link>
        <RailNav path={path} withRepo={withRepo} />
        <div className="rail__foot">
          {demo ? (
            <p className="rail__note">
              <span className="chip chip--info">
                <span className="chip__dot" aria-hidden="true" />
                Demo data
              </span>
              Numbers are examples until the API is connected.
            </p>
          ) : (
            <p className="rail__note">
              <span className="live-dot" aria-hidden="true" /> Connected to Brain
            </p>
          )}
        </div>
      </aside>
      <button className="scrim" type="button" aria-label="Close menu" tabIndex={drawer ? 0 : -1} onClick={() => setDrawer(false)} />

      <header className="mast">
        <button className="mast__menu icon-btn" type="button" aria-label="Open menu" onClick={() => setDrawer(true)}>
          <Icon name="menu" size={20} />
        </button>
        <p className="mast__where">
          {current && <Icon name={current.icon} />}
          <span>{current?.label ?? "Project Brain"}</span>
        </p>
        <div className="mast__tools">
          <div className="repo">
            <button
              type="button"
              className="repo__btn"
              aria-haspopup="listbox"
              aria-expanded={repoMenu}
              onClick={() => setRepoMenu((o) => !o)}
            >
              <span className={`dot tone-${repo?.tone}`} aria-hidden="true" />
              <span className="repo__label">
                <small>Repository</small>
                {repo?.name}
              </span>
              <Icon name="arrow" size={14} className="repo__caret" />
            </button>
            {repoMenu && (
              <ul className="repo__menu pop" role="listbox" aria-label="Switch repository">
                {repos.map((r, i) => (
                  <li key={r.slug} role="option" aria-selected={r.slug === repo?.slug} style={{ "--i": i } as CSSProperties}>
                    <Link href={withRepo(path, r.slug)} className={r.slug === repo?.slug ? "is-on" : ""}>
                      <span className={`dot tone-${r.tone}`} aria-hidden="true" />
                      <span className="repo__name">{r.name}</span>
                      <span className="repo__status">{r.status}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <button type="button" className="search-btn" onClick={() => setPalette(true)}>
            <Icon name="search" size={16} />
            <span className="search-btn__text">Jump to…</span>
            <kbd>Ctrl K</kbd>
          </button>
        </div>
      </header>

      <main id="main" className="main">
        {children}
      </main>

      {palette && (
        <Palette
          onClose={() => setPalette(false)}
          repos={repos}
          withRepo={withRepo}
          path={path}
        />
      )}
    </div>
  );
}

// The active marker is one element that travels between items, so a route
// change reads as movement from where you were to where you are.
function RailNav({ path, withRepo }: { path: string; withRepo: (href: string) => string }) {
  const ref = useRef<HTMLElement>(null);
  const [mark, setMark] = useState<{ y: number; h: number } | null>(null);
  const [ready, setReady] = useState(false);

  useLayoutEffect(() => {
    const el = ref.current?.querySelector<HTMLElement>("[aria-current='page']");
    setMark(el ? { y: el.offsetTop, h: el.offsetHeight } : null);
  }, [path]);
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
            const on = isActive(item.href, path);
            return (
              <Link
                key={item.key}
                href={withRepo(item.href)}
                className={`nav__item${on ? " is-on" : ""}`}
                aria-current={on ? "page" : undefined}
                title={item.hint}
              >
                <Icon name={item.icon} />
                {item.label}
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
  href: string;
  icon?: Parameters<typeof Icon>[0]["name"];
  tone?: Tone;
  group: string;
}

function Palette({
  onClose,
  repos,
  withRepo,
  path,
}: {
  onClose: () => void;
  repos: ShellRepo[];
  withRepo: (href: string, slug?: string) => string;
  path: string;
}) {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const listRef = useRef<HTMLUListElement>(null);

  const entries = useMemo<Entry[]>(() => {
    const pages: Entry[] = allNavItems.map((i) => ({
      id: i.key,
      label: i.label,
      hint: i.hint,
      href: withRepo(i.href),
      icon: i.icon,
      group: "Go to",
    }));
    const repoEntries: Entry[] = repos.map((r) => ({
      id: `repo-${r.slug}`,
      label: `Switch to ${r.name}`,
      hint: r.status,
      href: withRepo(path, r.slug),
      tone: r.tone,
      group: "Repository",
    }));
    const needle = q.trim().toLowerCase();
    return [...pages, ...repoEntries].filter(
      (e) => !needle || e.label.toLowerCase().includes(needle) || e.hint.toLowerCase().includes(needle),
    );
  }, [q, repos, withRepo, path]);

  useEffect(() => setSel(0), [q]);
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-i='${sel}']`)?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  const go = (e?: Entry) => {
    if (!e) return;
    router.push(e.href);
    onClose();
  };

  return (
    <div className="palette" role="dialog" aria-modal="true" aria-label="Jump to a screen">
      <button className="palette__scrim" type="button" aria-label="Close" onClick={onClose} tabIndex={-1} />
      <div className="palette__box">
        <div className="palette__field">
          <Icon name="search" size={18} />
          <input
            autoFocus
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Where do you want to go? Try “search” or “agents”"
            aria-label="Search screens and repositories"
            aria-controls="palette-list"
            aria-activedescendant={entries[sel] ? `pal-${entries[sel].id}` : undefined}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setSel((s) => Math.min(s + 1, entries.length - 1));
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
              className={`palette__row${i === sel ? " is-on" : ""}`}
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
            <kbd>Enter</kbd> open
          </span>
        </p>
      </div>
    </div>
  );
}
