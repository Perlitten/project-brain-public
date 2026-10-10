"use client";
// Setup, made of controls: every step opens to the thing that finishes it —
// start a check, save a path, pick a model, index, mint an agent key, build a
// first context pack. Each step says why it exists and where it stands in
// plain text; nothing important hides behind a hover.

import Link from "next/link";
import "./setup.css";
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { CodeBlock, LockNote, RunButton, useAction } from "@/components/act";
import { Icon } from "@/components/Icon";
import { KeyDialog } from "@/components/KeyDialog";
import { AgentConfigurations } from "@/components/setup/AgentConfigurations";
import { createAgentKey, type NewKey } from "@/lib/actions/admin";
import { startHealthCheck, startReindex } from "@/lib/actions/jobs";
import { buildFirstPack, saveProvider, saveRepoPath, verifyAgent, verifyProviders, type ProviderInput } from "@/lib/actions/setup";
import { shortRevision } from "@/lib/revision";
import { STEP_COPY } from "@/lib/setup-copy";
import type { ProviderPreset, ProviderSlot, SetupView, StepView } from "@/lib/setup-view";

type Json = Record<string, unknown>;
type ClientConfig = { name: string; where: string; config: string; cli: string };
const obj = (v: unknown): Json => (v && typeof v === "object" && !Array.isArray(v) ? (v as Json) : {});
const str = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : String(v));
const cap = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const sentence = (s: string) => (s ? cap(s.trim()).replace(/[.\s]*$/, ".") : s);

export function SetupSteps({ view, clients = [] }: { view: SetupView; clients?: ClientConfig[] }) {
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
    <ol className="steps">
      {view.steps.map((s, i) => (
        <Step key={s.id} step={s} index={i} isNext={s.id === view.nextStep} open={open === s.id} onToggle={() => setOpen(open === s.id ? null : s.id)}>
          {s.id === "services" && <Services step={s} />}
          {s.id === "repo" && <Repo step={s} repoPath={view.repoPath} />}
          {s.id === "provider" && <Providers step={s} view={view} />}
          {s.id === "indexed" && <Index step={s} repoPath={view.repoPath} />}
          {s.id === "agent" && <Agent step={s} apiUrl={view.publicApiUrl} repoPath={view.repoPath} clients={clients} />}
          {s.id === "first_task" && <FirstTask step={s} repoPath={view.repoPath} />}
        </Step>
      ))}
    </ol>
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
  const copy = STEP_COPY[step.id];
  const status = statusLine(step);

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
            <span className="step__title">{copy?.title ?? step.title}</span>
            {chip}
            <span className="setup-step__toggle" aria-hidden="true">
              {open ? "Hide" : step.done ? "Change" : "Open"}
              <Icon name="arrow" size={14} />
            </span>
          </button>
        </div>
        {copy && <p className="setup-step__why">{copy.why}</p>}
        {status && (
          <p className={`setup-step__status setup-step__status--${state}`}>
            {status.now && (
              <>
                <b>{step.done ? "Done" : "Now"}</b> {status.now}
              </>
            )}
            {status.next && (
              <>
                {" "}
                <b>Next</b> {status.next}
              </>
            )}
          </p>
        )}
        {open && <div className="setup-step__panel">{children}</div>}
      </div>
    </li>
  );
}

// Where a step stands, in a sentence. The API's detail strings are terse; the
// one that confused people most is the context-pack step, which stays undone
// after every re-index because older packs describe an older revision.
function statusLine(step: StepView): { now: string; next?: string } | null {
  const next = !step.done && step.hint ? sentence(step.hint) : undefined;
  if (step.id === "first_task" && !step.done && step.status !== "blocked") {
    const p = obj(step.extra.packs);
    const n = (k: string) => (typeof p[k] === "number" ? (p[k] as number) : 0);
    const old = n("stale");
    const broken = n("missing") + n("empty");
    const parts = [
      old > 0 && `${old.toLocaleString("en")} context pack${old === 1 ? " was" : "s were"} built before the latest index run, so ${old === 1 ? "it describes" : "they describe"} older code`,
      broken > 0 && `${broken.toLocaleString("en")} pack file${broken === 1 ? " is" : "s are"} missing on the server`,
    ].filter(Boolean) as string[];
    return parts.length
      ? { now: sentence(parts.join("; ")), next: "Build one for the current code below — that finishes this step." }
      : { now: "No context pack yet.", next: "Describe a task below and build one." };
  }
  if (!step.detail && !next) return null;
  return { now: sentence(step.detail), next };
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
        <span className="field__label">Folder on the Brain server</span>
        <input id="setup-repo-path" className="input input--mono" value={path} onChange={(e) => setPath(e.target.value)} placeholder="e.g. /home/you/code/my-project" spellCheck={false} autoComplete="off" />
        <span className="field__hint">An absolute path to a checkout on the machine Brain runs on, not on your laptop. Saved to the server’s .env.</span>
      </label>
      <div className="btn-row">
        <button type="submit" className="btn btn--primary" disabled={pending || mode !== "on" || !path.trim() || (!changed && step.done)}>
          {pending ? <span className="spin" aria-hidden="true" /> : <Icon name="check" size={15} />}
          Save folder
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

  return (
    <div className="form form--wide">
      <div className="prov-status">
        {(["llm", "embedding"] as Slot[]).map((s) => {
          const p = probe(s);
          const st = str(p.state);
          return (
            <button key={s} type="button" className="prov-status__item" aria-pressed={slot === s} onClick={() => setSlot(s)}>
              <span className="prov-status__slot">{s === "llm" ? "Language model — writes summaries" : "Embedding model — makes code searchable"}</span>
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
        <p className="field__hint">This Brain server is older than the model picker. Set LLM_* and EMBEDDING_* in the server’s .env, restart it, then press Verify both.</p>
      ) : (
        <ProviderForm key={slot} slot={slot} presets={cat.presets} current={slot === "llm" ? cat.llm : cat.embedding} />
      )}

      <div className="btn-row">
        <RunButton action={verifyProviders} label="Verify both" icon="check" title="Verify providers" variant={step.done ? "neutral" : "primary"} />
      </div>
      <p className="field__hint">Verify makes one real call to each model with the saved settings.</p>
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
          Now using <b>{current.provider}</b> through a built-in adapter. Pick a provider below to switch to an OpenAI-compatible endpoint.
        </p>
      )}
      <div className="field">
        <span className="field__label">
          Provider
          {slot === "embedding" && presets.length > usable.length && <em>only those that serve embeddings</em>}
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
            <span className="field__hint">
              Stored in the server’s .env and never shown again. {needsKey ? "Without a key this provider fails verification." : "This provider works without one."}
            </span>
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
              Save {slot === "llm" ? "language model" : "embedding model"}
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
            {run.commit ? ` · revision ${shortRevision(str(run.commit))}` : ""}
          </dd>
          <dt>Files</dt>
          <dd>
            {n(typeof files.indexed === "number" ? "indexed" : "processed")} read · {n(typeof files.unchanged === "number" ? "unchanged" : "skipped")} unchanged since last run · {n("failed")} failed
          </dd>
        </dl>
      )}
      <div className="btn-row">
        <RunButton
          action={() => startReindex({ repoPath })}
          label={hasRun ? "Read changes" : "Read the code now"}
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
              body: "Brain will re-read every file and rebuild its search index. Searches keep working on the old copy until it finishes; on a large repository this takes several minutes.",
              yes: "Start full re-read",
            }}
          />
        )}
        <Link className="btn btn--ghost" href="/indexing">
          All index runs
        </Link>
      </div>
      {hasRun && <p className="field__hint">Read changes only re-reads files that changed since the last run. Re-read everything starts from scratch.</p>}
      <LockNote />
    </div>
  );
}

// ---------------------------------------------------------------------------
// 5. Agent

function Agent({ step, apiUrl, repoPath, clients }: { step: StepView; apiUrl: string; repoPath?: string; clients: ClientConfig[] }) {
  const ext = obj(step.extra.external_client);
  const check = obj(step.extra.self_check);
  const [name, setName] = useState("my-agent");
  const [key, setKey] = useState<NewKey | null>(null);
  const { run, pending, mode } = useAction(createAgentKey, { title: "Agent key" });

  const seen = ext.connected ? `${str(ext.client) || "a client"}${ext.at ? ` · ${str(ext.at)}` : ""}` : "none yet — an agent shows up here after its first call to Brain";
  const checked = Object.keys(check).length ? `${str(check.status)}${step.extra.self_check_fresh === false ? " (outdated — run it again)" : ""}` : "not run";

  return (
    <div className="form form--wide">
      <dl className="kv">
        <dt>Connected agent</dt>
        <dd>{seen}</dd>
        <dt>Server self-check</dt>
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
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} aria-label="Key name" placeholder="e.g. claude-laptop" maxLength={120} />
        <button type="submit" className="btn btn--primary" disabled={pending || mode !== "on" || !name.trim()}>
          {pending ? <span className="spin" aria-hidden="true" /> : <Icon name="key" size={15} />}
          Create agent key
        </button>
        <RunButton action={verifyAgent} label="Run self-check" icon="check" title="Agent self-check" />
      </form>
      <p className="field__hint">
        Name the key after the machine or agent that will use it. You get a separate key that can be revoked, limited to agent permissions, plus a config to
        paste into your agent. Self-check calls the same tools from the server to prove they work. Every key is listed in{" "}
        <Link className="inline-link" href="/admin">
          Access
        </Link>
        .
      </p>
      <LockNote />
      {clients.length > 0 && (
        <details className="disclosure">
          <summary>Agent runs on the Brain server itself?</summary>
          <p>Then it can start Brain’s tools directly, without a key. The paths below are the server’s, not your laptop’s.</p>
          <AgentConfigurations clients={clients} />
        </details>
      )}

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
  const total = ["usable", "stale", "missing", "empty"].reduce((n, k) => n + (typeof packs[k] === "number" ? (packs[k] as number) : 0), 0);
  const length = task.trim().length;
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
        <span className="field__label">Task</span>
        <textarea
          id="setup-task"
          className="textarea"
          rows={3}
          value={task}
          onChange={(e) => setTask(e.target.value)}
          placeholder="e.g. Add rate limiting to the login endpoint"
          maxLength={2000}
        />
        <span className="field__hint">
          {length > 0 && length < 8
            ? `${8 - length} more character${8 - length === 1 ? "" : "s"} to go.`
            : "Write it like a ticket: what to change and where. Brain only reads your code; nothing is changed."}
        </span>
      </label>
      <div className="btn-row">
        <button type="submit" className="btn btn--primary" disabled={pending || mode !== "on" || length < 8}>
          {pending ? <span className="spin" aria-hidden="true" /> : <Icon name="layers" size={15} />}
          {pending ? "Building…" : "Build context pack"}
        </button>
        {total > 0 && (
          <Link className="btn btn--ghost" href="/packs">
            See all {total.toLocaleString("en")} context pack{total === 1 ? "" : "s"}
          </Link>
        )}
      </div>
      {built && (
        <div className="callout tone-ok">
          <b>Context pack ready{built.files?.length ? ` — ${built.files.length} files picked` : ""}.</b>
          {built.files && built.files.length > 0 && (
            <ul className="filelist">
              {built.files.map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ul>
          )}
          <Link className="inline-link" href="/packs">
            Open it in Context packs
          </Link>
        </div>
      )}
      <LockNote />
    </form>
  );
}
