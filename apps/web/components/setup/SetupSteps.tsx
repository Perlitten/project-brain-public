"use client";
// Get started, made of controls: every step opens to the thing that finishes
// it — start a check, save a path, pick a model, index, mint an agent key,
// build a first briefing. Done steps stay closed but open to change them.

import Link from "next/link";
import "./setup.css";
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { CodeBlock, LockNote, RunButton, useAction } from "@/components/act";
import { Icon } from "@/components/Icon";
import { KeyDialog } from "@/components/KeyDialog";
import { Tip } from "@/components/Tip";
import { createAgentKey, type NewKey } from "@/lib/actions/admin";
import { startHealthCheck, startReindex } from "@/lib/actions/jobs";
import { buildFirstPack, saveProvider, saveRepoPath, verifyAgent, verifyProviders, type ProviderInput } from "@/lib/actions/setup";
import type { ProviderPreset, ProviderSlot, SetupView, StepView } from "@/lib/setup-view";

type Json = Record<string, unknown>;
const obj = (v: unknown): Json => (v && typeof v === "object" && !Array.isArray(v) ? (v as Json) : {});
const str = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : String(v));
const cap = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

const SHORT: Record<string, string> = {
  services: "Services",
  repo: "Repository",
  provider: "Models",
  indexed: "Index",
  agent: "Agent",
  first_task: "First pack",
};

export function SetupSteps({ view }: { view: SetupView }) {
  // One step open at a time — the next one — so the page reads as a checklist.
  const [open, setOpen] = useState<string | null>(view.nextStep ?? null);
  const show = (id: string) => {
    setOpen(id);
    requestAnimationFrame(() => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };
  // A link to /setup#provider opens that step and scrolls to it.
  useEffect(() => {
    const go = () => {
      const id = location.hash.slice(1);
      if (view.steps.some((s) => s.id === id)) show(id);
    };
    go();
    window.addEventListener("hashchange", go);
    return () => window.removeEventListener("hashchange", go);
  }, [view.steps]);

  return (
    <>
      <StepBar steps={view.steps} next={view.nextStep} onPick={show} />
      <ol className="steps">
        {view.steps.map((s, i) => (
          <Step key={s.id} step={s} index={i} isNext={s.id === view.nextStep} open={open === s.id} onToggle={() => setOpen(open === s.id ? null : s.id)}>
            {s.id === "services" && <Services step={s} />}
            {s.id === "repo" && <Repo step={s} repoPath={view.repoPath} />}
            {s.id === "provider" && <Providers step={s} view={view} />}
            {s.id === "indexed" && <Index step={s} repoPath={view.repoPath} />}
            {s.id === "agent" && <Agent step={s} apiUrl={view.publicApiUrl} repoPath={view.repoPath} />}
            {s.id === "first_task" && <FirstTask step={s} repoPath={view.repoPath} />}
          </Step>
        ))}
      </ol>
    </>
  );
}

// One segment per step: green when done, teal for the next one.
function StepBar({ steps, next, onPick }: { steps: StepView[]; next?: string; onPick: (id: string) => void }) {
  const done = steps.filter((s) => s.done).length;
  return (
    <nav className="stepbar" aria-label={`Setup progress: ${done} of ${steps.length} done`}>
      {steps.map((s, i) => {
        const state = s.done ? "done" : s.id === next ? "next" : "todo";
        return (
          <button key={s.id} type="button" className={`stepbar__seg stepbar__seg--${state}`} onClick={() => onPick(s.id)} aria-label={`${i + 1}. ${s.title} — ${s.done ? "done" : "not done"}`}>
            <span className="stepbar__fill" aria-hidden="true" />
            <span className="stepbar__label">
              {s.done ? <Icon name="check" size={12} /> : <span className="num">{i + 1}</span>}
              {SHORT[s.id] ?? s.title}
            </span>
            <span className="stepbar__tip" role="tooltip">
              <b>{s.title}</b>
              {s.detail && <span>{cap(s.detail)}.</span>}
            </span>
          </button>
        );
      })}
    </nav>
  );
}

// ---------------------------------------------------------------------------

function Step({ step, index, isNext, open, onToggle, children }: { step: StepView; index: number; isNext: boolean; open: boolean; onToggle: () => void; children: ReactNode }) {
  const state = step.done ? "done" : isNext ? "next" : "todo";
  const chip = step.done ? null : isNext ? (
    <span className="chip chip--info">
      <span className="chip__dot" aria-hidden="true" />
      Next
    </span>
  ) : step.status === "blocked" ? (
    <span className="chip chip--idle">Waiting</span>
  ) : (
    <span className="chip chip--warn">
      <span className="chip__dot" aria-hidden="true" />
      To do
    </span>
  );
  const help = [step.detail && cap(step.detail), step.hint && !step.done && `Next: ${step.hint}`].filter(Boolean).join(". ");

  return (
    <li id={step.id} className={`step step--${state} setup-step${open ? " is-open" : ""}`} style={{ "--i": index } as CSSProperties}>
      <span className="step__badge" aria-label={step.done ? "Done" : `Step ${index + 1}`}>
        {step.done ? (
          <svg className="step__check" width={18} height={18} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <polyline points="4 12.5 9.5 18 20 6" pathLength={1} />
          </svg>
        ) : (
          index + 1
        )}
      </span>
      <div className="setup-step__body">
        <div className="setup-step__row">
          <button type="button" className="setup-step__head" aria-expanded={open} onClick={onToggle}>
            <span className="step__title">{step.title}</span>
            {chip}
            <span className="setup-step__toggle" aria-hidden="true">
              <Icon name="arrow" size={14} />
            </span>
          </button>
          {help && (
            <Tip label="Status details" end>
              {help}.
            </Tip>
          )}
        </div>
        {open && <div className="setup-step__panel">{children}</div>}
      </div>
    </li>
  );
}

// ---------------------------------------------------------------------------
// 1. Services

function Services({ step }: { step: StepView }) {
  const services = obj(step.extra.services);
  const names = Object.keys(services);
  return (
    <div className="form form--wide">
      {names.length > 0 && (
        <div className="svc-chips">
          {names.map((n) => {
            const s = str(services[n]) || "unknown";
            const tone = s === "ok" || s === "healthy" ? "ok" : s === "disabled" || s === "skipped" ? "idle" : "bad";
            return (
              <span key={n} className={`chip chip--${tone}`} title={`${n}: ${s}`}>
                <span className="chip__dot" aria-hidden="true" />
                {n}
                {tone !== "ok" && <em className="svc-chips__state"> · {s}</em>}
              </span>
            );
          })}
        </div>
      )}
      {!step.done && step.hint && <CodeBlock label="Start them on the Brain server" code={step.hint} />}
      <div className="btn-row">
        <RunButton action={startHealthCheck} label="Check again" icon="refresh" title="Health check" />
        <Link className="btn btn--ghost" href="/logs">
          See activity
        </Link>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 2. Repository

function Repo({ step, repoPath }: { step: StepView; repoPath?: string }) {
  const [path, setPath] = useState(repoPath ?? "");
  const { run, pending, mode } = useAction(saveRepoPath, { title: "Repository" });
  const changed = path.trim() !== (repoPath ?? "");
  return (
    <form
      className="form"
      onSubmit={(e) => {
        e.preventDefault();
        run(path);
      }}
    >
      <label className="field" htmlFor="setup-repo-path">
        <span className="field__label">
          Repository folder
          <Tip>A path on the Brain server. Brain reads the checkout in place — nothing is copied. Saved to the server’s .env.</Tip>
        </span>
        <input id="setup-repo-path" className="input input--mono" value={path} onChange={(e) => setPath(e.target.value)} placeholder="/home/you/code/my-project" spellCheck={false} autoComplete="off" />
      </label>
      <div className="btn-row">
        <button type="submit" className="btn btn--primary" disabled={pending || mode !== "on" || !path.trim() || (!changed && step.done)}>
          {pending ? <span className="spin" aria-hidden="true" /> : <Icon name="check" size={15} />}
          Save repository
        </button>
      </div>
      <LockNote />
    </form>
  );
}

// ---------------------------------------------------------------------------
// 3. Providers

type Slot = "llm" | "embedding";

interface Draft {
  provider: string;
  baseUrl: string;
  model: string;
  summarizerModel: string;
  dimension: string;
  apiKey: string;
  clearApiKey: boolean;
}

function draftFrom(slot: Slot, cur?: ProviderSlot): Draft {
  return {
    provider: cur && !cur.native ? cur.provider : "",
    baseUrl: cur?.baseUrl ?? "",
    model: cur?.model ?? "",
    summarizerModel: slot === "llm" ? (cur?.summarizerModel ?? "") : "",
    dimension: slot === "embedding" && cur?.dimension ? String(cur.dimension) : "",
    apiKey: "",
    clearApiKey: false,
  };
}

const STATE_TONE: Record<string, "ok" | "warn" | "bad" | "idle" | "info"> = {
  verified: "ok",
  configured: "warn",
  missing: "bad",
  failed: "bad",
  demo: "idle",
};
const STATE_WORD: Record<string, string> = {
  verified: "works",
  configured: "saved, not verified",
  missing: "key missing",
  failed: "failed",
  demo: "demo mode",
};

function Providers({ step, view }: { step: StepView; view: SetupView }) {
  const [slot, setSlot] = useState<Slot>("llm");
  const cat = view.providers;
  const probe = (s: Slot) => obj(step.extra[s]);
  const verify = (
    <RunButton action={verifyProviders} label="Verify both" icon="check" title="Verify providers" variant={step.done ? "neutral" : "primary"} />
  );

  return (
    <div className="form form--wide">
      <div className="prov-status">
        {(["llm", "embedding"] as Slot[]).map((s) => {
          const p = probe(s);
          const st = str(p.state);
          return (
            <button key={s} type="button" className="prov-status__item" aria-pressed={slot === s} onClick={() => setSlot(s)}>
              <span className="prov-status__slot">{s === "llm" ? "Language model" : "Embeddings"}</span>
              <span className="prov-status__name">{str(p.provider) || "—"}</span>
              {st && (
                <span className={`chip chip--${STATE_TONE[st] ?? "idle"}`} title={str(p.detail)}>
                  <span className="chip__dot" aria-hidden="true" />
                  {STATE_WORD[st] ?? st}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {!cat ? (
        <p className="field__hint">
          Picker needs a server update
          <Tip>
            This Brain server is older than the provider picker. Until it is updated, set LLM_* / EMBEDDING_* in the server’s .env, restart it and press
            Verify.
          </Tip>
        </p>
      ) : (
        <ProviderForm key={slot} slot={slot} presets={cat.presets} current={slot === "llm" ? cat.llm : cat.embedding} />
      )}

      <div className="btn-row">
        {verify}
        <Tip>Makes one real call to each model with the saved settings.</Tip>
      </div>
      <LockNote />
    </div>
  );
}

function ProviderForm({ slot, presets, current }: { slot: Slot; presets: ProviderPreset[]; current?: ProviderSlot }) {
  const [d, setD] = useState<Draft>(() => draftFrom(slot, current));
  const [notes, setNotes] = useState<string[]>([]);
  const set = (patch: Partial<Draft>) => setD((x) => ({ ...x, ...patch }));
  const { run, pending, mode } = useAction(saveProvider, { title: slot === "llm" ? "Language model" : "Embeddings" });
  const preset = presets.find((p) => p.name === d.provider);
  const keySaved = Boolean(current?.keySet) && current?.provider === d.provider;
  const needsKey = preset ? preset.requiresKey : true;
  const usable = presets.filter((p) => (slot === "embedding" ? p.embeddings : true));

  const pick = (p: ProviderPreset) =>
    set({
      provider: p.name,
      baseUrl: p.baseUrl ?? "",
      model: (slot === "llm" ? p.llmModel : p.embeddingModel) ?? "",
      summarizerModel: slot === "llm" ? (p.summarizerModel ?? "") : "",
      dimension: slot === "embedding" && p.embeddingDimension ? String(p.embeddingDimension) : "",
      apiKey: "",
      clearApiKey: false,
    });

  const submit = async () => {
    const input: ProviderInput = {
      slot,
      provider: d.provider,
      baseUrl: d.baseUrl.trim(),
      model: d.model.trim(),
      apiKey: d.apiKey,
      clearApiKey: d.clearApiKey,
    };
    if (slot === "llm") input.summarizerModel = d.summarizerModel.trim();
    if (slot === "embedding") input.dimension = d.dimension.trim() ? Number(d.dimension) : 0;
    const res = await run(input);
    if (res.ok) {
      setD((x) => ({ ...x, apiKey: "", clearApiKey: false }));
      setNotes(res.data?.notes ?? []);
    }
  };

  const badUrl = d.baseUrl.trim() !== "" && !/^https?:\/\/[^\s]+$/.test(d.baseUrl.trim());
  const badDim = d.dimension.trim() !== "" && !/^\d{1,5}$/.test(d.dimension.trim());

  return (
    <form
      className="form form--wide"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      autoComplete="off"
    >
      {current?.native && (
        <p className="field__hint">
          Now: <b>{current.provider}</b>
          <Tip>Runs through a built-in adapter. Pick a provider below to switch to an OpenAI-compatible endpoint.</Tip>
        </p>
      )}
      <div className="field">
        <span className="field__label">
          Provider
          {slot === "embedding" && presets.length > usable.length && <Tip>Only providers that serve embeddings are listed.</Tip>}
        </span>
        <div className="presets" role="group" aria-label="Provider">
          {usable.map((p) => (
            <button key={p.name} type="button" className="preset" aria-pressed={d.provider === p.name} onClick={() => pick(p)}>
              <b>{p.label}</b>
              <small>{p.requiresKey ? "needs a key" : "no key needed"}</small>
            </button>
          ))}
        </div>
      </div>

      {d.provider && (
        <>
          <div className="form__row">
            <label className="field">
              <span className="field__label">
                Base URL <em>{preset?.baseUrl ? "blank = preset default" : "required"}</em>
              </span>
              <input className="input input--mono" value={d.baseUrl} onChange={(e) => set({ baseUrl: e.target.value })} placeholder={preset?.baseUrl ?? "https://host/v1"} spellCheck={false} />
              {badUrl && <span className="field__hint field__hint--bad">Use a full http(s):// address.</span>}
            </label>
            <label className="field">
              <span className="field__label">
                Model <em>blank = preset default</em>
              </span>
              <input
                className="input input--mono"
                value={d.model}
                onChange={(e) => set({ model: e.target.value })}
                placeholder={(slot === "llm" ? preset?.llmModel : preset?.embeddingModel) ?? "model-name"}
                spellCheck={false}
              />
            </label>
            {slot === "llm" ? (
              <label className="field">
                <span className="field__label">
                  Summarizer model <em>optional, cheaper</em>
                </span>
                <input className="input input--mono" value={d.summarizerModel} onChange={(e) => set({ summarizerModel: e.target.value })} placeholder={preset?.summarizerModel ?? "same as model"} spellCheck={false} />
              </label>
            ) : (
              <label className="field">
                <span className="field__label">
                  Vector size <em>blank = model default</em>
                </span>
                <input className="input input--mono" inputMode="numeric" value={d.dimension} onChange={(e) => set({ dimension: e.target.value })} placeholder={preset?.embeddingDimension ? String(preset.embeddingDimension) : "1536"} />
                {badDim && <span className="field__hint field__hint--bad">A whole number, e.g. 1024.</span>}
              </label>
            )}
          </div>

          <div className="field">
            <span className="field__label">
              <label htmlFor={`provider-${slot}-key`}>API key</label>
              <Tip>Write-only: stored in the server’s .env and never shown again. {needsKey ? "Without a key this provider fails verification." : "This provider works without one."}</Tip>
            </span>
            <input
              id={`provider-${slot}-key`}
              className="input input--mono"
              type="password"
              value={d.apiKey}
              onChange={(e) => set({ apiKey: e.target.value, clearApiKey: false })}
              placeholder={keySaved ? `Saved${current?.keySource ? ` (${current.keySource})` : ""} — leave blank to keep it` : needsKey ? "Paste the provider’s API key" : "Not needed for this provider"}
              autoComplete="new-password"
              spellCheck={false}
            />
            {keySaved && (
              <label className="check">
                <input type="checkbox" checked={d.clearApiKey} onChange={(e) => set({ clearApiKey: e.target.checked, apiKey: "" })} />
                Remove the saved key
              </label>
            )}
          </div>

          <div className="btn-row">
            <button type="submit" className="btn btn--primary" disabled={pending || mode !== "on" || badUrl || badDim || (!preset?.baseUrl && !d.baseUrl.trim())}>
              {pending ? <span className="spin" aria-hidden="true" /> : <Icon name="check" size={15} />}
              Save {slot === "llm" ? "language model" : "embeddings"}
            </button>
          </div>
        </>
      )}

      {notes.length > 0 && (
        <div className="callout tone-info">
          <b>Saved. Before it takes full effect:</b>
          {notes.map((n) => (
            <span key={n}>{n}</span>
          ))}
        </div>
      )}
    </form>
  );
}

// ---------------------------------------------------------------------------
// 4. Index

function Index({ step, repoPath }: { step: StepView; repoPath?: string }) {
  const run = obj(step.extra.run);
  const files = obj(run.files);
  const hasRun = Object.keys(run).length > 0;
  const n = (k: string) => (typeof files[k] === "number" ? (files[k] as number).toLocaleString("en") : "—");
  return (
    <div className="form form--wide">
      {hasRun && (
        <dl className="kv">
          <dt>Last run</dt>
          <dd>
            #{str(run.run_id)} · {str(run.status)}
            {run.commit ? ` · commit ${str(run.commit).replace(/^snapshot:/, "").slice(0, 7)}` : ""}
          </dd>
          <dt>Files</dt>
          <dd>
            {n(typeof files.indexed === "number" ? "indexed" : "processed")} read · {n(typeof files.unchanged === "number" ? "unchanged" : "skipped")} unchanged · {n("failed")} failed
          </dd>
        </dl>
      )}
      <div className="btn-row">
        <RunButton
          action={() => startReindex({ repoPath })}
          label={hasRun ? "Index changes" : "Index now"}
          icon="play"
          variant={step.done ? "neutral" : "primary"}
          title="Index"
        />
        {hasRun && (
          <RunButton
            action={() => startReindex({ repoPath, clean: true })}
            label="Re-read everything"
            icon="refresh"
            title="Full re-index"
            confirm={{
              title: "Re-read the whole repository?",
              body: "Brain will re-read every file and rebuild its vectors. Searches keep working on the old copy until it finishes; on a large repository this takes several minutes.",
              yes: "Start full re-index",
            }}
          />
        )}
        <Link className="btn btn--ghost" href="/indexing">
          History
        </Link>
      </div>
      <LockNote />
    </div>
  );
}

// ---------------------------------------------------------------------------
// 5. Agent

function Agent({ step, apiUrl, repoPath }: { step: StepView; apiUrl: string; repoPath?: string }) {
  const ext = obj(step.extra.external_client);
  const check = obj(step.extra.self_check);
  const [name, setName] = useState("my-agent");
  const [key, setKey] = useState<NewKey | null>(null);
  const { run, pending, mode } = useAction(createAgentKey, { title: "Agent key" });

  const seen = ext.connected ? `${str(ext.client) || "a client"}${ext.at ? ` · ${str(ext.at)}` : ""}` : "not yet";
  const checked = Object.keys(check).length ? `${str(check.status)}${step.extra.self_check_fresh === false ? " (stale)" : ""}` : "not run";

  return (
    <div className="form form--wide">
      <dl className="kv">
        <dt>Agent seen</dt>
        <dd>
          {seen}
          {!ext.connected && <Tip>An agent shows up here after its first Brain tool call.</Tip>}
        </dd>
        <dt>Self-check</dt>
        <dd>{checked}</dd>
      </dl>

      <form
        className="inline-form"
        onSubmit={async (e) => {
          e.preventDefault();
          const res = await run(name, undefined, undefined);
          if (res.ok && res.data) setKey(res.data);
        }}
      >
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} aria-label="Agent name" placeholder="claude-laptop" maxLength={120} />
        <button type="submit" className="btn btn--primary" disabled={pending || mode !== "on" || !name.trim()}>
          {pending ? <span className="spin" aria-hidden="true" /> : <Icon name="key" size={15} />}
          Create agent key
        </button>
        <RunButton action={verifyAgent} label="Run self-check" icon="check" title="Agent self-check" />
        <Tip>
          The key is separate and revocable, with just the agent permissions (core:write, jobs:read); you get a ready config to paste. Self-check calls the
          agent tools on the server. All keys live in Access.
        </Tip>
      </form>
      <LockNote />

      <KeyDialog k={key} apiUrl={apiUrl} repoPath={repoPath} onClose={() => setKey(null)} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// 6. First task

function FirstTask({ step, repoPath }: { step: StepView; repoPath?: string }) {
  const packs = obj(step.extra.packs);
  const [task, setTask] = useState("");
  const [built, setBuilt] = useState<{ files?: string[]; id?: number } | null>(null);
  const { run, pending, mode } = useAction(buildFirstPack, { title: "Context pack" });
  const usable = typeof packs.usable === "number" ? packs.usable : 0;
  return (
    <form
      className="form form--wide"
      onSubmit={async (e) => {
        e.preventDefault();
        const res = await run(task, repoPath);
        if (res.ok) setBuilt(res.data ?? {});
      }}
    >
      <label className="field" htmlFor="setup-task">
        <span className="field__label">
          A real task for an agent
          <Tip>Brain picks the files, rules and decisions an agent needs for it — the same briefing your agent will get.</Tip>
        </span>
        <textarea
          id="setup-task"
          className="textarea"
          rows={3}
          value={task}
          onChange={(e) => setTask(e.target.value)}
          placeholder="Add rate limiting to the login endpoint"
          maxLength={2000}
        />
      </label>
      <div className="btn-row">
        <button type="submit" className="btn btn--primary" disabled={pending || mode !== "on" || task.trim().length < 8}>
          {pending ? <span className="spin" aria-hidden="true" /> : <Icon name="layers" size={15} />}
          {pending ? "Building…" : "Build briefing"}
        </button>
        {usable > 0 && (
          <Link className="btn btn--ghost" href="/packs">
            See {usable} briefing{usable === 1 ? "" : "s"}
          </Link>
        )}
      </div>
      {built && (
        <div className="callout tone-ok">
          <b>Briefing ready{built.files?.length ? ` — ${built.files.length} files picked` : ""}.</b>
          {built.files && built.files.length > 0 && (
            <ul className="filelist">
              {built.files.map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ul>
          )}
          <Link className="inline-link" href="/packs">
            Open it in Context packs →
          </Link>
        </div>
      )}
      <LockNote />
    </form>
  );
}
