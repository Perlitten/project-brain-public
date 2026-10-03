"use client";
// Settings, made of controls: each section edits its slice of the server's
// .env (only what changed is sent), Telegram and n8n show their live status
// next to their fields with a one-click test. Help lives in "?" tips.

import Link from "next/link";
import "./settings.css";
import { useState, type ReactNode } from "react";
import { LockNote, RunButton, useAction } from "@/components/act";
import { Icon } from "@/components/Icon";
import { Tip } from "@/components/Tip";
import { Chip, Panel } from "@/components/ui";
import { saveSettings, testN8n, testTelegram } from "@/lib/actions/settings";
import type { N8nStatus, SettingField, SettingsSection, SettingsView, TelegramStatus } from "@/lib/settings-view";
import type { Tone } from "@/lib/types";

type Draft = Record<string, string | boolean>;

const initial = (f: SettingField): string | boolean => (f.kind === "bool" ? f.value === true : f.kind === "secret" || f.value == null ? "" : String(f.value));

export function Settings({ view }: { view: SettingsView }) {
  const live = view.mode === "live" || view.mode === "demo";
  const section = (id: string) => view.sections.find((s) => s.id === id);
  const publicN8n = section("n8n")?.fields.find((f) => f.key === "N8N_PUBLIC_URL")?.value;

  return (
    <div className="settings">
      <nav className="settings__toc" aria-label="Sections">
        {[
          ["telegram", "Telegram"],
          ["n8n", "n8n"],
          ...view.sections.filter((s) => s.id !== "telegram" && s.id !== "n8n").map((s) => [s.id, s.title]),
          ["models", "Models"],
        ].map(([id, label]) => (
          <a key={id} href={`#${id}`}>
            {label}
          </a>
        ))}
      </nav>

      <Panel
        id="telegram"
        title="Telegram alerts"
        actions={live && <RunButton action={testTelegram} label="Send test" icon="play" small title="Telegram" />}
      >
        <TelegramLine t={view.telegram} />
        {live ? <SectionForm section={section("telegram")} /> : <EnvHint keys={["TELEGRAM_ALERTS_ENABLED", "TELEGRAM_ALERT_BOT_TOKEN", "TELEGRAM_ALERT_CHAT_ID"]} />}
      </Panel>

      <Panel
        id="n8n"
        title="n8n automation"
        actions={
          <div className="btn-row">
            {typeof publicN8n === "string" && publicN8n && (
              <a className="btn btn--neutral btn--sm" href={publicN8n} target="_blank" rel="noreferrer">
                Open n8n <Icon name="arrow" size={13} />
              </a>
            )}
            {live && <RunButton action={testN8n} label="Test" icon="refresh" small title="n8n" />}
          </div>
        }
      >
        <N8nLine n={view.n8n} />
        {live ? <SectionForm section={section("n8n")} /> : <EnvHint keys={["N8N_BASE_URL", "N8N_API_KEY"]} />}
      </Panel>

      {view.sections
        .filter((s) => s.id !== "telegram" && s.id !== "n8n")
        .map((s) => (
          <Panel key={s.id} id={s.id} title={s.title}>
            {s.id === "automation" && view.llmError && (
              <p className="field__hint field__hint--bad settings__warn">
                Last diagnosis LLM call failed: {view.llmError}
                <Tip>
                  Fix the language model in <Link href="/setup#provider">Get started → Models</Link>, or turn off “Summarize diagnosis with the LLM”.
                </Tip>
              </p>
            )}
            <SectionForm section={s} />
          </Panel>
        ))}

      <Panel
        id="models"
        title="Models"
        actions={
          <Link className="btn btn--neutral btn--sm" href="/setup#provider">
            Change <Icon name="arrow" size={13} />
          </Link>
        }
      >
        {view.models ? <Models m={view.models} /> : <p className="field__hint">Shown once the server is updated.</p>}
      </Panel>
      <LockNote />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Status lines

function TelegramLine({ t }: { t: TelegramStatus | null }) {
  if (!t) return null;
  const [tone, label]: [Tone, string] = t.enabled && t.credentials ? ["ok", "On"] : t.credentials ? ["idle", "Off"] : ["warn", "Not set up"];
  return (
    <div className="status-line">
      <Chip tone={tone}>{label}</Chip>
      {t.last ? (
        <span>
          Last alert: <b>{t.last.status}</b>
          {t.last.at && ` · ${t.last.at}`}
          {t.last.findings != null && ` · ${t.last.findings} finding${t.last.findings === 1 ? "" : "s"}`}
        </span>
      ) : (
        <span className="muted">No alerts sent yet</span>
      )}
      <Tip end>
        Create a bot with @BotFather (/newbot) to get the token. Add it to your chat, write something there, then open
        api.telegram.org/bot&lt;token&gt;/getUpdates — the chat id is in the reply.
      </Tip>
    </div>
  );
}

const API_LABEL: Record<string, [Tone, string]> = {
  available: ["ok", "API key works"],
  missing_key: ["warn", "No API key"],
  auth_failed: ["bad", "API key rejected"],
  unreachable: ["bad", "API unreachable"],
};

function N8nLine({ n }: { n: N8nStatus | null }) {
  if (!n) return null;
  const up = n.health === "healthy";
  const [apiTone, apiLabel] = API_LABEL[n.apiStatus] ?? (["idle", n.apiStatus] as [Tone, string]);
  const active = n.workflows.filter((w) => w.active).length;
  return (
    <>
      <div className="status-line">
        <Chip tone={up ? "ok" : n.health === "unknown" ? "idle" : "bad"}>{up ? "Reachable" : n.health}</Chip>
        <Chip tone={apiTone}>{apiLabel}</Chip>
        {n.url && <code>{n.url}</code>}
        {n.apiStatus === "missing_key" && (
          <Tip end>Without a key Brain can’t tell which workflows are switched on. In n8n: Settings → n8n API → Create an API key, then paste it below.</Tip>
        )}
      </div>
      {n.workflows.length > 0 && (
        <details className="wf">
          <summary>
            {n.workflows.length} workflow{n.workflows.length === 1 ? "" : "s"}
            {n.apiStatus === "available" ? ` · ${active} active` : n.source === "repo" ? " in the repo · state unknown" : ""}
          </summary>
          <ul>
            {n.workflows.map((w) => (
              <li key={w.name}>
                <span className="dot" style={{ ["--tone" as string]: w.active ? "var(--ok-fg)" : "var(--text-3)" }} aria-hidden="true" />
                {w.name}
              </li>
            ))}
          </ul>
        </details>
      )}
    </>
  );
}

function EnvHint({ keys }: { keys: string[] }) {
  return (
    <p className="field__hint">
      Editing here needs a server update.
      <Tip>
        Until then set {keys.join(", ")} in the server’s .env and restart: <code>docker compose up -d</code>.
      </Tip>
    </p>
  );
}

function Models({ m }: { m: Record<string, string | number | null> }) {
  const row = (label: string, provider: unknown, model: unknown, extra?: ReactNode) => (
    <>
      <dt>{label}</dt>
      <dd>
        <b>{String(provider ?? "—")}</b>
        {model ? <code> {String(model)}</code> : null}
        {extra}
      </dd>
    </>
  );
  return (
    <dl className="kv">
      {row("Language model", m.llm_provider, m.llm_model)}
      {m.summarizer_model ? row("Summarizer", m.llm_provider, m.summarizer_model) : null}
      {row("Embeddings", m.embedding_provider, m.embedding_model, m.embedding_dimension ? <span className="muted"> · {m.embedding_dimension} dims</span> : null)}
    </dl>
  );
}

// ---------------------------------------------------------------------------
// One section's form

function problem(f: SettingField, v: string | boolean): string | null {
  if (typeof v === "boolean" || f.kind === "secret") return null;
  const s = v.trim();
  if (!s) return f.clearable ? null : "Required.";
  if (f.kind === "int" || f.kind === "float") {
    const n = Number(s);
    if (!Number.isFinite(n) || (f.kind === "int" && !Number.isInteger(n))) return f.kind === "int" ? "A whole number." : "A number.";
    if (f.min != null && n < f.min) return `At least ${f.min}.`;
    if (f.max != null && n > f.max) return `At most ${f.max}.`;
  }
  if (f.kind === "url" && !/^https?:\/\/[^\s/?#@]+(\/[^\s?#]*)?$/.test(s)) return "A full http(s):// address.";
  return null;
}

function SectionForm({ section }: { section?: SettingsSection }) {
  const fields = section?.fields ?? [];
  const [d, setD] = useState<Draft>(() => Object.fromEntries(fields.map((f) => [f.key, initial(f)])));
  const [clear, setClear] = useState<string[]>([]);
  const [notes, setNotes] = useState<string[]>([]);
  const { run, pending, mode } = useAction(saveSettings, { title: section?.title ?? "Settings" });
  if (!section || !fields.length) return null;

  const set = (key: string, v: string | boolean) => {
    setD((x) => ({ ...x, [key]: v }));
    setClear((c) => c.filter((k) => k !== key));
  };
  const toggleClear = (key: string, on: boolean) => {
    setClear((c) => (on ? [...c.filter((k) => k !== key), key] : c.filter((k) => k !== key)));
    if (on) setD((x) => ({ ...x, [key]: "" }));
  };

  const values: Record<string, string | number | boolean> = {};
  const clears = [...clear];
  for (const f of fields) {
    const v = d[f.key] ?? initial(f);
    if (f.kind === "secret") {
      if (typeof v === "string" && v.trim()) values[f.key] = v.trim();
    } else if (v !== initial(f)) {
      if (typeof v === "boolean") values[f.key] = v;
      else if (!v.trim()) f.clearable && clears.push(f.key);
      else values[f.key] = f.kind === "int" || f.kind === "float" ? Number(v) : v.trim();
    }
  }
  const dirty = Object.keys(values).length + clears.length;
  const bad = fields.some((f) => problem(f, d[f.key] ?? initial(f)));

  const submit = async () => {
    const res = await run(values, clears);
    if (res.ok) {
      setD((x) => Object.fromEntries(Object.entries(x).map(([k, v]) => [k, fields.find((f) => f.key === k)?.kind === "secret" ? "" : v])));
      setClear([]);
      setNotes(res.data?.notes ?? []);
    }
  };

  const toggles = fields.filter((f) => f.kind === "bool");
  const inputs = fields.filter((f) => f.kind !== "bool");

  return (
    <form
      className="form form--wide settings-form"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      autoComplete="off"
    >
      {toggles.length > 0 && (
        <ul className="toggles">
          {toggles.map((f) => (
            <li key={f.key}>
              <label className="switch">
                <input type="checkbox" role="switch" checked={d[f.key] === true} onChange={(e) => set(f.key, e.target.checked)} />
                <span className="switch__track" aria-hidden="true" />
                <span>{f.label}</span>
              </label>
              <Hints f={f} />
            </li>
          ))}
        </ul>
      )}

      {inputs.length > 0 && (
        <div className="form__row">
          {inputs.map((f) => (
            <Input key={f.key} f={f} v={String(d[f.key] ?? "")} cleared={clear.includes(f.key)} onChange={(v) => set(f.key, v)} onClear={(on) => toggleClear(f.key, on)} />
          ))}
        </div>
      )}

      <div className="btn-row">
        <button type="submit" className="btn btn--primary btn--sm" disabled={pending || mode !== "on" || !dirty || bad}>
          {pending ? <span className="spin" aria-hidden="true" /> : <Icon name="check" size={14} />}
          {dirty ? `Save ${dirty} change${dirty === 1 ? "" : "s"}` : "Saved"}
        </button>
        {dirty > 0 && !pending && (
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => {
              setD(Object.fromEntries(fields.map((f) => [f.key, initial(f)])));
              setClear([]);
            }}
          >
            Undo
          </button>
        )}
      </div>

      {notes.length > 0 && (
        <div className="callout tone-info">
          {notes.map((n) => (
            <span key={n}>{n}</span>
          ))}
        </div>
      )}
    </form>
  );
}

function Hints({ f }: { f: SettingField }) {
  return (
    <>
      {f.help && <Tip end>{f.help}</Tip>}
      {f.fromEnv && (
        <span className="env-badge">
          env
          <Tip end>Also set in the server’s process environment (e.g. docker compose). That value wins after a restart — change it there too.</Tip>
        </span>
      )}
    </>
  );
}

function Input({ f, v, cleared, onChange, onClear }: { f: SettingField; v: string; cleared: boolean; onChange: (v: string) => void; onClear: (on: boolean) => void }) {
  const err = problem(f, v);
  const secret = f.kind === "secret";
  const num = f.kind === "int" || f.kind === "float";
  return (
    <div className="field">
      <label className="field__label" htmlFor={`set-${f.key}`}>
        {f.label}
        {num && f.min != null && f.max != null && (
          <em>
            {f.min}–{f.max}
          </em>
        )}
        <Hints f={f} />
      </label>
      <input
        id={`set-${f.key}`}
        className={`input${secret || f.kind === "url" || num ? " input--mono" : ""}`}
        type={secret ? "password" : "text"}
        inputMode={num ? "numeric" : undefined}
        value={v}
        onChange={(e) => onChange(e.target.value)}
        placeholder={secret ? (cleared ? "Will be removed" : f.set ? "Saved — leave blank to keep" : "Not set") : f.clearable ? "Not set" : ""}
        autoComplete={secret ? "new-password" : "off"}
        spellCheck={false}
      />
      {err && <span className="field__hint field__hint--bad">{err}</span>}
      {secret && f.set && (
        <label className="check check--sm">
          <input type="checkbox" checked={cleared} onChange={(e) => onClear(e.target.checked)} />
          Remove
        </label>
      )}
    </div>
  );
}
