"use client";
// Access → Dashboard sign-in. A fresh install on 127.0.0.1 (or with WEB_OPEN_MODE=true)
// is open and the owner sets a login and password here. Once set, changing or removing it takes the current
// password. Works without the actions gate: setting the first password is
// exactly what unlocks the rest of the dashboard.

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { Dialog, report } from "@/components/act";
import { Icon } from "@/components/Icon";
import { Chip, Panel } from "@/components/ui";
import { disableWebLogin, setWebLogin } from "@/lib/actions/login";
import type { ActionResult } from "@/lib/actions/types";
import type { SignInView } from "@/lib/sign-in-view";
import { SIGN_IN_STATUS } from "./sign-in-status";

const USER = /^[A-Za-z0-9._@+-]{1,64}$/;
const MIN = 8;


export function SignIn({ view }: { view: SignInView }) {
  const s = SIGN_IN_STATUS[view.mode];
  const editable = view.mode === "open" || view.mode === "password";
  return (
    <Panel id="sign-in" title="Dashboard sign-in" desc="The login and password for this web dashboard. Agents and integrations use their own keys below." spine={s.tone}>
      <div className="signin">
        <div className="signin__state">
          <Chip tone={s.tone}>{s.chip}</Chip>
          <b>{s.title}</b>
          <p>
            {view.mode === "open" &&
              "No password is set. That is fine while the dashboard listens on this machine only; set a login and password before anyone else can reach it. This browser stays signed in."}
            {view.mode === "password" && (
              <>
                Signed in as <code>{view.user}</code>. Changing the login or password signs every other browser out.
              </>
            )}
            {view.mode === "env" && (
              <>
                The login comes from <code>WEB_BASIC_AUTH</code> in the dashboard’s environment. Change it there and redeploy; remove it to manage
                sign-in here instead.
              </>
            )}
            {view.mode === "team" && (
              <>
                This Brain has team members, so the shared login no longer opens the dashboard. Each member signs in with their own key, and
                Brain checks it on every request. Manage members on <a href="/team">Team</a>.
              </>
            )}
            {view.mode === "demo" && "This dashboard shows example data. Sign-in appears here once it is connected to a Brain server."}
            {view.mode === "unknown" && "The dashboard can’t learn the sign-in state — check that Brain is running, or that WEB_BASIC_AUTH looks like user:password."}
          </p>
          {editable && (
            <p className="field__hint">
              Forgot the password? On the Brain server delete the <code>WEB_LOGIN_…</code> lines from <code>.env</code> and restart the API, then
              open the dashboard on this machine (it listens on 127.0.0.1 by default) to set a new one (or use <code>WEB_BASIC_AUTH</code>).
            </p>
          )}
          {view.pinned.length > 0 && (
            <p className="field__hint field__hint--bad">
              {view.pinned.join(", ")} {view.pinned.length === 1 ? "is" : "are"} also set in the API’s environment (docker compose) and will override
              changes made here after a restart. Remove {view.pinned.length === 1 ? "it" : "them"} there.
            </p>
          )}
        </div>
        {editable && <LoginForm view={view} />}
      </div>
    </Panel>
  );
}

function LoginForm({ view }: { view: SignInView }) {
  const router = useRouter();
  const first = view.mode === "open";
  const [user, setUser] = useState(view.user ?? "");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [current, setCurrent] = useState("");
  const [removing, setRemoving] = useState(false);
  const [pending, start] = useTransition();

  const userBad = user.length > 0 && !USER.test(user);
  const shortPw = password.length > 0 && password.length < MIN;
  const edgePw = password.length > 0 && password.trim() !== password;
  const mismatch = repeat.length > 0 && repeat !== password;
  const ready = USER.test(user) && password.length >= MIN && !edgePw && repeat === password && (first || current.length > 0);

  const finish = (res: ActionResult) => {
    report(res, "Dashboard sign-in");
    if (res.ok) {
      setPassword("");
      setRepeat("");
      setCurrent("");
      setRemoving(false);
      router.refresh();
    }
  };
  const save = () =>
    start(async () => {
      try {
        finish(await setWebLogin({ user, password, currentPassword: first ? undefined : current }));
      } catch {
        finish({ ok: false, message: "The dashboard couldn’t reach its server. Reload and try again." });
      }
    });
  const remove = () =>
    start(async () => {
      try {
        finish(await disableWebLogin({ currentPassword: current }));
      } catch {
        finish({ ok: false, message: "The dashboard couldn’t reach its server. Reload and try again." });
      }
    });

  return (
    <>
    <form
      className="form form--wide signin__form"
      onSubmit={(e) => {
        e.preventDefault();
        if (ready && !pending) save();
      }}
    >
      <div className="form__row">
        <label className="field">
          <span className="field__label">Login</span>
          <input className="input" value={user} onChange={(e) => setUser(e.target.value.trim())} autoComplete="username" spellCheck={false} maxLength={64} placeholder="owner" />
          <span className={`field__hint${userBad ? " field__hint--bad" : ""}`}>Letters, digits and . _ @ + -</span>
        </label>
        {!first && (
          <label className="field">
            <span className="field__label">Current password</span>
            <Secret value={current} onChange={setCurrent} autoComplete="current-password" />
            <span className="field__hint">Proves it’s you.</span>
          </label>
        )}
      </div>
      <div className="form__row">
        <label className="field">
          <span className="field__label">{first ? "Password" : "New password"}</span>
          <Secret value={password} onChange={setPassword} autoComplete="new-password" />
          <span className={`field__hint${shortPw || edgePw ? " field__hint--bad" : ""}`}>
            {edgePw ? "No spaces at the start or end." : `At least ${MIN} characters.`}
          </span>
        </label>
        <label className="field">
          <span className="field__label">Repeat it</span>
          <Secret value={repeat} onChange={setRepeat} autoComplete="new-password" />
          <span className={`field__hint${mismatch ? " field__hint--bad" : ""}`}>{mismatch ? "Doesn’t match yet." : "Same again, to catch typos."}</span>
        </label>
      </div>
      <div className="btn-row">
        <button type="submit" className="btn btn--primary" disabled={!ready || pending}>
          {pending ? <span className="spin" aria-hidden="true" /> : <Icon name="key" size={15} />}
          {first ? "Set password and lock" : "Save new login"}
        </button>
        {!first && (
          <button type="button" className="btn btn--ghost btn--sm signin__remove" onClick={() => setRemoving(true)} disabled={pending}>
            Remove password…
          </button>
        )}
      </div>
    </form>
      {!first && (
        <Dialog open={removing} onClose={() => setRemoving(false)} title="Remove the password?">
          <p className="dialog__text">Sign-in becomes open: anyone who can reach this address can use the dashboard, and actions lock again.</p>
          <label className="field">
            <span className="field__label">Current password</span>
            <Secret value={current} onChange={setCurrent} autoComplete="current-password" autoFocusInDialog />
          </label>
          <div className="btn-row">
            <button type="button" className="btn btn--danger" disabled={!current || pending} onClick={remove}>
              Remove password
            </button>
            <button type="button" className="btn btn--neutral" onClick={() => setRemoving(false)}>
              Keep it
            </button>
          </div>
        </Dialog>
      )}
    </>
  );
}

// A password input with an eye button that shows what was typed.
function Secret({ value, onChange, autoComplete, autoFocusInDialog }: { value: string; onChange: (v: string) => void; autoComplete: string; autoFocusInDialog?: boolean }) {
  const [shown, setShown] = useState(false);
  return (
    <span className="secret">
      <input
        className="input"
        type={shown ? "text" : "password"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoComplete={autoComplete}
        spellCheck={false}
        {...(autoFocusInDialog ? { "data-autofocus": "" } : {})}
      />
      <button
        type="button"
        className="secret__eye"
        onClick={() => setShown((v) => !v)}
        aria-pressed={shown}
        aria-label={shown ? "Hide password" : "Show password"}
        title={shown ? "Hide password" : "Show password"}
      >
        <Icon name={shown ? "eye-off" : "eye"} size={16} />
      </button>
    </span>
  );
}
