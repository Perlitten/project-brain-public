"use client";
// Everyday controls first; connection details and deployment overrides are
// disclosed separately. Only changed settings are sent to the server.

import Link from "next/link";
import "./settings.css";
import { useState, type ReactNode } from "react";
import { LockNote, RunButton, useAction } from "@/components/act";
import { Icon } from "@/components/Icon";
import { Panel } from "@/components/ui";
import { saveSettings, testTelegram } from "@/lib/actions/settings";
import type { SettingField, SettingsSection, SettingsView, TelegramStatus } from "@/lib/settings-view";
import { SchedulerLine, SchedulerTable } from "./Scheduler";

type Draft = Record<string, string | boolean>;

const initial = (f: SettingField): string | boolean => (f.kind === "bool" ? f.value === true : f.kind === "secret" || f.value == null ? "" : String(f.value));

export function Settings({ view }: { view: SettingsView }) {
  const live = view.mode === "live" || view.mode === "demo";
  const section = (id: string) => view.sections.find((s) => s.id === id);

  return (
    <div className="settings">
      <nav className="settings__toc" aria-label="Sections">
        {[
          ["telegram", "Telegram"],
          ["scheduler", "Scheduler"],
          ...view.sections.filter((s) => s.id !== "telegram").map((s) => [s.id, s.title]),
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
        actions={live && <RunButton action={testTelegram} label="Send test message" icon="play" small title="Telegram" />}
      >
        {live ? <SectionForm section={section("telegram")} telegram={view.telegram} /> : (
          <><TelegramLine t={view.telegram} /><EnvHint keys={["TELEGRAM_ALERTS_ENABLED", "TELEGRAM_ALERT_BOT_TOKEN", "TELEGRAM_ALERT_CHAT_ID"]} /></>
        )}
      </Panel>

      <Panel id="scheduler" title="Scheduled jobs" flush>
        <div className="settings__pad">
          <SchedulerLine s={view.scheduler} />
        </div>
        {view.scheduler && <SchedulerTable s={view.scheduler} />}
      </Panel>

      {view.sections
        .filter((s) => s.id !== "telegram")
        .map((s) => (
          <Panel key={s.id} id={s.id} title={s.title}>
            {s.id === "automation" && view.llmError && (
              <p className="field__hint field__hint--bad settings__warn">
                Last diagnosis LLM call failed: {view.llmError}. Fix the language model in{" "}
                <Link href="/setup#provider">Setup → Connect AI models</Link>, or turn off “Summarize diagnosis with the LLM”.
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
  const sent = t.last?.status === "sent";
  return (
    <div className="telegram-delivery">
      <span className="telegram-delivery__label">Last message</span>
      <p>{sent ? "Delivered" : t.last ? t.last.status.replaceAll("_", " ") : "No messages yet"}{t.last?.at && ` on ${t.last.at}`}</p>
      {t.last?.findings != null && <span className="field__hint">{t.last.findings} finding{t.last.findings === 1 ? "" : "s"}</span>}
    </div>
  );
}

function EnvHint({ keys }: { keys: string[] }) {
  return (
    <p className="field__hint">
      Editing here needs a server update. Until then set {keys.join(", ")} in the server’s .env and restart with{" "}
      <code>docker compose up -d</code>.
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

function SectionForm({ section, telegram }: { section?: SettingsSection; telegram?: TelegramStatus | null }) {
  const fields = section?.fields ?? [];
  const [d, setD] = useState<Draft>(() => Object.fromEntries(fields.map((f) => [f.key, initial(f)])));
  const [clear, setClear] = useState<string[]>([]);
  const [notes, setNotes] = useState<string[]>([]);
  const [resetVersion, setResetVersion] = useState(0);
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
    if (pending || mode !== "on" || !dirty || bad) return;
    const res = await run(values, clears);
    if (res.ok) {
      setD((x) => Object.fromEntries(Object.entries(x).map(([k, v]) => [k, fields.find((f) => f.key === k)?.kind === "secret" ? "" : v])));
      setClear([]);
      setNotes(res.data?.notes ?? []);
    }
  };

  const toggles = fields.filter((f) => f.kind === "bool");
  const inputs = fields.filter((f) => f.kind !== "bool");
  const isTelegram = section.id === "telegram";
  const cooldown = inputs.find((f) => f.key === "TELEGRAM_ALERT_COOLDOWN_SECONDS");
  const connectionFields = inputs.filter((f) => f !== cooldown);
  const changedOverrides = fields.filter((f) => f.fromEnv && (Object.hasOwn(values, f.key) || clears.includes(f.key)));
  const renderInput = (f: SettingField, quiet = false) => (
    <Input key={f.key} f={f} v={String(d[f.key] ?? "")} cleared={clear.includes(f.key)} onChange={(v) => set(f.key, v)} onClear={(on) => toggleClear(f.key, on)} quiet={quiet} />
  );

  return (
    <form
      className="form form--wide settings-form"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      autoComplete="off"
    >
      {isTelegram ? (
        <>
          <div className="telegram-overview">
            <div className="telegram-controls">
              {toggles.map((f) => (
                <div key={f.key}>
                  <label className="switch telegram-switch">
                    <input type="checkbox" role="switch" checked={d[f.key] === true} aria-describedby={`set-${f.key}-help`} onChange={(e) => set(f.key, e.target.checked)} />
                    <span className="switch__track" aria-hidden="true" />
                    <span>{f.key === "TELEGRAM_ALERTS_ENABLED" ? "Send Telegram alerts" : f.label}</span>
                  </label>
                  <p id={`set-${f.key}-help`} className="field__hint">Send findings from background checks to your Telegram chat.</p>
                </div>
              ))}
              {!telegram?.credentials && <p className="field__hint field__hint--bad">Connect a bot below to receive alerts.</p>}
            </div>
            <TelegramLine t={telegram ?? null} />
          </div>
          {cooldown && <CooldownInput key={resetVersion} f={cooldown} v={String(d[cooldown.key] ?? "")} onChange={(v) => set(cooldown.key, v)} />}
          {connectionFields.length > 0 && (
            <details className="settings-disclosure" open={!telegram?.credentials || undefined}>
              <summary>Bot connection <span className="settings-disclosure__state">{telegram?.credentials ? "Configured" : "Setup required"}</span></summary>
              <div className="settings-disclosure__body">
                <p className="field__hint">Create a bot with @BotFather, then add its token and the chat that should receive alerts.</p>
                <div className="form__row">{connectionFields.map((f) => renderInput({ ...f, label: settingLabel(f), help: connectionHelp[f.key] ?? f.help }, true))}</div>
              </div>
            </details>
          )}
        </>
      ) : <>
      {toggles.length > 0 && (
        <ul className="toggles">
          {toggles.map((f) => (
            <li key={f.key}>
              <label className="switch">
                <input type="checkbox" role="switch" checked={d[f.key] === true} onChange={(e) => set(f.key, e.target.checked)} />
                <span className="switch__track" aria-hidden="true" />
                <span>{f.label}</span>
              </label>
              {f.help && <p className="field__hint">{f.help}</p>}
            </li>
          ))}
        </ul>
      )}

      {inputs.length > 0 && (
        <div className="form__row">
          {inputs.map((f) => renderInput(f))}
        </div>
      )}
      </>}

      <ServerOverrides fields={fields} />
      {changedOverrides.length > 0 && (
        <p className="settings-save-warning" role="status">
          Server configuration also sets {changedOverrides.map(settingLabel).join(", ")}. After saving, update that configuration too: a restart can restore the old values.
        </p>
      )}

      {(dirty > 0 || pending) && <div className="btn-row settings-actions" role="group" aria-label="Unsaved changes">
        <button type="submit" className="btn btn--primary btn--sm" disabled={pending || mode !== "on" || !dirty || bad}>
          {pending ? <span className="spin" aria-hidden="true" /> : <Icon name="check" size={14} />}
          {pending ? "Saving…" : "Save changes"}
        </button>
        {dirty > 0 && !pending && (
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => {
              setD(Object.fromEntries(fields.map((f) => [f.key, initial(f)])));
              setClear([]);
              setResetVersion((version) => version + 1);
            }}
          >
            Discard changes
          </button>
        )}
      </div>}

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

function ServerOverrides({ fields }: { fields: SettingField[] }) {
  const overridden = fields.filter((f) => f.fromEnv);
  if (!overridden.length) return null;
  return (
    <details className="settings-disclosure settings-disclosure--technical">
      <summary>Server configuration</summary>
      <div className="settings-disclosure__body">
        <p className="field__hint">These settings are also defined by the server deployment. Changes here apply to the API immediately, but the deployment values can return after a restart. Update the deployment configuration to keep your changes.</p>
        <dl className="settings-sources">{overridden.map((f) => <div key={f.key}><dt>{settingLabel(f)}</dt><dd><code>{f.key}</code></dd></div>)}</dl>
      </div>
    </details>
  );
}

const alertIntervals = [[60, "1 minute"], [900, "15 minutes"], [3600, "1 hour"], [21600, "6 hours"], [43200, "12 hours"], [86400, "1 day"], [604800, "7 days"]] as const;
const connectionHelp: Record<string, string> = {
  TELEGRAM_ALERT_BOT_TOKEN: "Get a token from @BotFather. Leave blank to keep your saved token.",
  TELEGRAM_ALERT_CHAT_ID: "The chat or channel where the bot sends notifications.",
  BRAIN_TELEGRAM_DEFAULT_REPO: "Used when a message to the bot does not name a repository.",
};
function settingLabel(f: SettingField): string {
  if (f.key === "TELEGRAM_ALERTS_ENABLED") return "Send Telegram alerts";
  if (f.key === "TELEGRAM_ALERT_COOLDOWN_SECONDS") return "Repeat interval";
  if (f.key === "BRAIN_TELEGRAM_DEFAULT_REPO") return "Default repository";
  return f.label;
}

function CooldownInput({ f, v, onChange }: { f: SettingField; v: string; onChange: (v: string) => void }) {
  const [custom, setCustom] = useState(() => !alertIntervals.some(([seconds]) => String(seconds) === v));
  const err = problem(f, v);
  const id = `set-${f.key}`;
  return (
    <div className="telegram-interval">
      <div>
        <label htmlFor={id}>Repeat interval</label>
        <p id={`${id}-help`} className="field__hint">Wait before sending the same findings again.</p>
      </div>
      <div className="telegram-interval__control">
        <select id={id} className="select" value={custom ? "custom" : v} aria-describedby={`${id}-help`} onChange={(e) => {
          setCustom(e.target.value === "custom");
          if (e.target.value !== "custom") onChange(e.target.value);
        }}>
          {alertIntervals.filter(([seconds]) => (f.min == null || seconds >= f.min) && (f.max == null || seconds <= f.max)).map(([seconds, label]) => <option key={seconds} value={seconds}>{label}</option>)}
          <option value="custom">Custom interval</option>
        </select>
        {custom && <div className="field">
          <label className="field__label" htmlFor={`${id}-custom`}>Interval in seconds</label>
          <input id={`${id}-custom`} className="input" type="number" min={f.min} max={f.max} step="1" value={v} aria-invalid={Boolean(err)} aria-describedby={err ? `${id}-error` : `${id}-help`} onChange={(e) => onChange(e.target.value)} />
          <span className="field__hint">From 1 minute to 7 days.</span>
        </div>}
        {err && <span id={`${id}-error`} className="field__hint field__hint--bad" role="status">{err}</span>}
      </div>
    </div>
  );
}

function Input({ f, v, cleared, onChange, onClear, quiet = false }: { f: SettingField; v: string; cleared: boolean; onChange: (v: string) => void; onClear: (on: boolean) => void; quiet?: boolean }) {
  const err = problem(f, v);
  const secret = f.kind === "secret";
  const num = f.kind === "int" || f.kind === "float";
  return (
    <div className="field">
      <div className="field__label">
        <label htmlFor={`set-${f.key}`}>{f.label}</label>
        {!quiet && num && f.min != null && f.max != null && (
          <em>
            {f.min}–{f.max}
          </em>
        )}
      </div>
      <input
        id={`set-${f.key}`}
        aria-invalid={Boolean(err)}
        aria-describedby={[err && `set-${f.key}-error`, f.help && `set-${f.key}-help`].filter(Boolean).join(" ") || undefined}
        className={`input${secret || f.kind === "url" || num ? " input--mono" : ""}`}
        type={secret ? "password" : "text"}
        inputMode={num ? "numeric" : undefined}
        value={v}
        onChange={(e) => onChange(e.target.value)}
        placeholder={secret ? (cleared ? "Will be removed" : f.set ? "Saved — leave blank to keep" : "Not set") : f.clearable ? "Not set" : ""}
        autoComplete={secret ? "new-password" : "off"}
        spellCheck={false}
      />
      {f.help && <span id={`set-${f.key}-help`} className="field__hint">{f.help}</span>}
      {err && <span id={`set-${f.key}-error`} className="field__hint field__hint--bad" role="status">{err}</span>}
      {secret && f.set && (
        <label className="check check--sm">
          <input type="checkbox" checked={cleared} aria-label={`Remove ${f.label}`} onChange={(e) => onClear(e.target.checked)} />
          Remove
        </label>
      )}
    </div>
  );
}
