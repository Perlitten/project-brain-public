// The page an invitee opens from an invitation link (/invite#token=pbi_…).
// proxy.ts answers it before any layout renders, like the sign-in page, so an
// anonymous visitor never reaches live data, server actions or the server key.
// Three steps, matching the invitation's lifecycle in Brain:
//   1 Token     the link's token, checked for shape here;
//   2 Accept    Brain redeems it and creates the member (state "verified");
//   3 Signed in the dashboard signs in with the new member key (state "active").

import { escape, gateDocument, type GateDoc } from "./login-page";
import { INVITE_PATH, TOKEN_RE, type AcceptOutcome } from "./invitations";

/** Another way to join, offered beside the token form (e.g. single sign-on). */
export interface InviteAlternative {
  label: string;
  href: string;
  /** Hand the token in the field to ``href`` as a POSTed ``invitation`` (never in the URL). */
  invitation?: boolean;
}

export type InviteNotice = { eyebrow: string; title: string; detail: string; status?: number };

export interface InvitePageInput {
  /** Brain's answer; absent before anything was submitted. */
  outcome?: AcceptOutcome;
  /** After "accepted" or "already": this browser now carries that member's session. */
  signedIn?: boolean;
  /** Invitations can't be redeemed here at all (demo data, misconfigured, switched off, wrong origin). */
  notice?: InviteNotice;
  /** Put back into the field after a failure that wasn't the token's fault. */
  token?: string;
  alternatives?: InviteAlternative[];
}

type Step = "todo" | "current" | "done" | "failed";
const STEP_NAMES = ["Token", "Accept", "Signed in"] as const;

function steps(state: [Step, Step, Step]) {
  const words: Record<Step, string> = { todo: "to do", current: "now", done: "done", failed: "stopped" };
  return `<ol class="steps" aria-label="Joining the team">${state
    .map((s, i) => `<li class="step step--${s}"${s === "current" ? ' aria-current="step"' : ""}><span class="step__n" aria-hidden="true">${s === "done" ? "✓" : s === "failed" ? "!" : i + 1}</span><span class="step__name">${STEP_NAMES[i]}</span><span class="sr">: ${words[s]}</span></li>`)
    .join("")}</ol>`;
}

function form(token: string, alternatives: InviteAlternative[]) {
  const alt = alternatives.length
    ? `<div class="alt"><span class="alt__or">or</span>${alternatives.map((a) => `<a class="btn btn--ghost" href="${escape(a.href)}"${a.invitation ? " data-invitation" : ""}>${escape(a.label)}</a>`).join("")}</div>`
    : "";
  return `<form method="post" action="${INVITE_PATH}" id="invite" autocomplete="off">
      <label class="field"><span class="field__label">Invitation token</span>
        <textarea id="token" name="token" rows="3" required spellcheck="false" autocapitalize="none" autocomplete="off" placeholder="pbi_…" aria-describedby="token-hint">${escape(token)}</textarea>
        <span class="field__hint" id="token-hint">Opened from the link? It’s filled in already. Otherwise paste the token that starts with <code>pbi_</code>.</span>
      </label>
      <button class="btn" type="submit">Accept invitation</button>
      ${alt}
    </form>`;
}

function keyBox(key: string) {
  return `<div class="keybox">
      <span class="field__label" id="key-label">Your member key</span>
      <div class="keybox__row">
        <code class="keybox__key" id="member-key" aria-labelledby="key-label">${escape(key)}</code>
        <button class="btn btn--ghost keybox__copy" type="button" data-copy="member-key">Copy</button>
      </div>
      <p class="keybox__note">Shown only this once. Store it in a password manager: you’ll need it to sign in on another browser, when this session ends after 7 days, and for the CLI and MCP.</p>
    </div>`;
}

const link = (href: string, label: string, ghost = false) => `<a class="btn${ghost ? " btn--ghost" : ""}" href="${escape(href)}">${label}</a>`;

/** Everything about the page except the shared document shell. */
function view({ outcome, signedIn = false, notice, token = "", alternatives = [] }: InvitePageInput): Omit<GateDoc, "pageTitle" | "extraCss" | "extraScript"> & { status: number } {
  if (notice) {
    return { status: notice.status ?? 503, tone: "bad", eyebrow: escape(notice.eyebrow), heading: escape(notice.title), detail: escape(notice.detail), alert: true, body: "",
      log: ["› invitation link opened", "› invitations unavailable here"], pulse: 0 };
  }
  if (!outcome) {
    return { status: 200, tone: "idle", eyebrow: "Invitation", heading: "Join this Brain’s team.",
      detail: "Brain checks your invitation, makes you a member and signs you in with your own key.",
      body: steps(["current", "todo", "todo"]) + form(token, alternatives), log: ["› invitation link opened", "› waiting for the token"] };
  }
  switch (outcome.kind) {
    case "accepted": {
      const who = escape(outcome.name);
      const admin = outcome.admin ? " You’re a team administrator." : "";
      if (signedIn) {
        return { status: 200, tone: "ok", eyebrow: "Welcome", heading: `You’re in, ${who}.`,
          detail: `You’re a member of this Brain’s team and signed in on this browser.${admin}`,
          body: steps(["done", "done", "done"]) + keyBox(outcome.key) + link("/", "Open the dashboard"),
          log: ["› invitation accepted", "› member created", "› signed in"], pulse: 140 };
      }
      return { status: 200, tone: "warn", eyebrow: "Member created", heading: `You’re a member, ${who}.`, alert: true,
        detail: `Brain accepted the invitation, but this dashboard couldn’t sign you in just now.${admin} Store the key below, then sign in with it.`,
        body: steps(["done", "done", "failed"]) + keyBox(outcome.key) + link("/", "Go to sign-in"),
        log: ["› invitation accepted", "› member created", "› sign-in pending"] };
    }
    case "already":
      if (signedIn) {
        return { status: 200, tone: "ok", eyebrow: "Already accepted", heading: "This invitation is already yours.",
          detail: `You’re signed in as ${escape(outcome.name)}. Brain showed your member key once, when you accepted; it isn’t shown again.`,
          body: steps(["done", "done", "done"]) + link("/", "Open the dashboard"), log: ["› invitation already accepted", "› signed in"] };
      }
      return { status: 409, tone: "warn", eyebrow: "Already used", heading: "This invitation was already accepted.", alert: true,
        detail: `It made ${escape(outcome.name)} a member. Brain shows the member key only once, so sign in with the key you saved. Lost it? Ask a team admin to revoke that member and invite you again.`,
        body: steps(["done", "done", "current"]) + link("/", "Go to sign-in"), log: ["› invitation already accepted", "› sign in with your saved key"] };
    case "malformed":
      return { status: 400, tone: "bad", eyebrow: "Not a token", heading: "That isn’t an invitation token.", alert: true,
        detail: "Invitation tokens start with pbi_. Open the link again, or copy the whole token.",
        body: steps(["failed", "todo", "todo"]) + form("", alternatives), log: ["› invitation link opened", "› token unreadable"], pulse: 92 };
    case "rejected":
      return { status: 403, tone: "bad", eyebrow: "Not accepted", heading: "That invitation didn’t work.", alert: true,
        detail: "The token is wrong, has expired or was revoked; Brain doesn’t say which, on purpose. Check that you copied all of it, or ask a team admin for a new invitation.",
        body: steps(["failed", "todo", "todo"]) + form("", alternatives), log: ["› invitation link opened", "› invitation refused"], pulse: 92 };
    case "throttled":
      return { status: 429, tone: "bad", eyebrow: "Paused", heading: "Too many attempts.", alert: true,
        detail: "Brain has paused invitation checks for a few minutes. Your invitation wasn’t used: wait, then try again.",
        body: steps(["done", "failed", "todo"]) + form(token, alternatives), log: ["› invitation link opened", "› too many tries · paused"], pulse: 92 };
    case "conflict":
      return { status: 409, tone: "warn", eyebrow: "Not yet", heading: "Brain couldn’t accept this invitation.", alert: true,
        detail: `${escape(outcome.message.trim().replace(/[.\s]+$/, ""))}. The invitation wasn’t used; a team admin can sort this out, then try again.`,
        body: steps(["done", "failed", "todo"]) + form(token, alternatives), log: ["› invitation link opened", "› accept refused"] };
    case "unavailable":
      return { status: 503, tone: "bad", eyebrow: "Unavailable", heading: "Brain isn’t answering.", alert: true,
        detail: "Brain may or may not have accepted the invitation before the connection failed. Try again in a minute. If it then says the invitation was already used and you never received a member key, ask a team admin to revoke that member and invite you again.",
        body: steps(["done", "failed", "todo"]) + form(token, alternatives), log: ["› invitation link opened", "› brain unreachable"], pulse: 92 };
  }
}

const CSS = `
.verdict--ok { --tone: var(--teal-fg); }
.verdict--warn { --tone: #f2c46d; }
.verdict--warn .eyebrow { color: #f2c46d; }
.verdict--ok .eyebrow { color: var(--teal-fg); }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.steps { position: relative; z-index: 3; margin: 0; padding: 0; list-style: none; display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.step { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-3); padding-top: 8px; border-top: 2px solid var(--gray-5); }
.step__n { display: inline-grid; place-items: center; width: 20px; height: 20px; border-radius: 50%; font: 500 11px / 1 var(--mono); background: var(--gray-4); color: var(--text-3); flex: none; }
.step--current { color: var(--text-1); border-top-color: var(--tone); }
.step--current .step__n { background: var(--tone); color: var(--gray-1); }
.step--done { color: var(--text-2); border-top-color: var(--teal-fg); }
.step--done .step__n { background: color-mix(in oklab, var(--teal-fg) 22%, var(--gray-4)); color: var(--teal-fg); }
.step--failed { color: var(--bad-fg); border-top-color: var(--bad-fg); }
.step--failed .step__n { background: color-mix(in oklab, var(--bad-fg) 22%, var(--gray-4)); color: var(--bad-fg); }
.verdict > .btn { position: relative; z-index: 3; text-decoration: none; }
textarea {
  width: 100%; min-height: 76px; padding: 10px 12px; border-radius: 4px; resize: vertical;
  background: var(--gray-4); color: var(--text-1); border: 1px solid var(--border-subtle);
  font: 400 13px / 1.5 var(--mono); outline: none; word-break: break-all;
}
textarea:hover { border-color: var(--border-default); }
textarea:focus-visible { border-color: var(--teal-fg); box-shadow: 0 0 0 3px color-mix(in oklab, var(--teal-fg) 16%, transparent); }
.field__hint { font-size: 12px; line-height: 1.5; color: var(--text-3); }
code { font: 400 12px / 1.5 var(--mono); }
.btn { text-decoration: none; }
.btn--ghost { background: transparent; color: var(--text-1); border-color: var(--border-default); }
.btn--ghost:hover:not(:disabled) { background: var(--gray-5); }
.btn--ghost:active:not(:disabled) { background: var(--gray-4); }
.alt { display: grid; gap: 10px; justify-items: stretch; }
.alt__or { font-size: 12px; color: var(--text-3); text-align: center; }
.keybox { position: relative; z-index: 3; display: grid; gap: 8px; }
.keybox__row { display: flex; gap: 8px; align-items: stretch; }
.keybox__key { flex: 1; min-width: 0; padding: 10px 12px; border-radius: 4px; background: var(--gray-1); border: 1px solid var(--border-default); color: var(--teal-fg); word-break: break-all; user-select: all; }
.keybox__copy { margin-top: 0; height: auto; min-height: 42px; }
.keybox__note { margin: 0; font-size: 13px; line-height: 1.5; color: var(--text-2); }
`;

const SCRIPT = `(function () {
  var field = document.getElementById("token");
  var RE = /${TOKEN_RE.source}/;
  if (location.hash) {
    var raw = location.hash.slice(1), value = raw;
    raw.split("&").forEach(function (part) { var at = part.indexOf("="); if (at > 0 && part.slice(0, at) === "token") value = part.slice(at + 1); });
    try { value = decodeURIComponent(value).trim(); } catch (e) { value = ""; }
    if (field && RE.test(value) && !field.value) field.value = value;
    // The token is a secret: keep it out of the address bar, history and bookmarks.
    history.replaceState(null, "", location.pathname + location.search);
  }
  // Single sign-on with the token from the field, POSTed so it stays out of URLs and logs; without one it is a plain sign-in.
  document.querySelectorAll("a[data-invitation]").forEach(function (a) {
    a.addEventListener("click", function (e) {
      var value = field ? field.value.trim() : "";
      if (!RE.test(value)) return;
      e.preventDefault();
      var post = document.createElement("form"), input = document.createElement("input");
      post.method = "post"; post.action = a.getAttribute("href");
      input.type = "hidden"; input.name = "invitation"; input.value = value;
      post.appendChild(input); document.body.appendChild(post); post.submit();
    });
  });
  var form = document.getElementById("invite");
  if (form) form.addEventListener("submit", function (e) {
    var b = form.querySelector("button[type=submit]");
    if (field) field.value = field.value.trim();
    b.disabled = true; b.textContent = "Accepting…";
    document.documentElement.setAttribute("data-going", "");
  });
  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var el = document.getElementById(button.getAttribute("data-copy"));
      var text = el ? el.textContent : "";
      function done(ok) { button.textContent = ok ? "Copied" : "Select and copy"; setTimeout(function () { button.textContent = "Copy"; }, 2000); }
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
      else {
        var range = document.createRange(); range.selectNodeContents(el);
        var sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
        var ok = false; try { ok = document.execCommand("copy"); } catch (e) {}
        done(ok);
      }
    });
  });
})();`;

/** The invite page as HTML plus the status to send it with. */
export function invitePage(input: InvitePageInput = {}): { status: number; html: string } {
  const { status, ...doc } = view(input);
  return { status, html: gateDocument({ ...doc, pageTitle: "Join the team", extraCss: CSS, extraScript: SCRIPT }) };
}
