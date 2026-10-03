// Sign-in screen served by proxy.ts while the site is locked with WEB_BASIC_AUTH.
// It is a self-contained HTML string on purpose: the root layout fetches live
// repository data, so the gate must answer before any page or layout renders.
// Values mirror app/globals.css (Ledger tokens, mark, ambient, verdict, pulse,
// boot log); the mark geometry comes straight from the shared BrandMark.

import { MARK_TOKENS } from "@/components/Icon";

const escape = (s: string) =>
  s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

const MARK = MARK_TOKENS.map(
  (t) =>
    `<rect class="mark__tok mark__tok--${t.side}${t.teal ? " mark__tok--teal" : ""}" x="${t.x}" y="${t.y}" width="${t.w}" height="6" rx="1" style="--row:${t.row};--k:${t.k}"/>`,
).join("");

// Same trace as the verdict pulse in components/ui.tsx: flat while the door is
// simply locked, racing after a refused attempt.
function ecg(period: number, width = 1200, base = 24) {
  if (!period) return `M 0 ${base} H ${width}`;
  let d = `M 0 ${base}`;
  for (let x = 0; x < width; x += period) {
    const u = period / 22;
    d +=
      ` H ${x + u * 6} q ${u} -4 ${u * 2} 0 H ${x + u * 10}` +
      ` l ${u * 0.6} 3 l ${u * 0.9} -21 l ${u} 27 l ${u * 0.7} -9` +
      ` H ${x + u * 15} q ${u * 1.6} -7 ${u * 3.2} 0 H ${x + period}`;
  }
  return d;
}

const LIT = 13;
const BRICKS = 20;

export function loginPage({ action, next, failed, user = "" }: { action: string; next: string; failed: boolean; user?: string }) {
  const tone = failed ? "bad" : "idle";
  const d = ecg(failed ? 92 : 0);
  const log = ["› waking up project brain", "› site is locked", failed ? "› sign-in refused · try again" : "› waiting for sign-in"];
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#05080B">
<meta name="robots" content="noindex">
<title>Sign in · Project Brain</title>
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="preload" href="/fonts/Outfit-600-latin.woff2" as="font" type="font/woff2" crossorigin>
<style>
@font-face { font-family: "Outfit"; src: url(/fonts/Outfit-400-latin.woff2) format("woff2"); font-weight: 400; font-display: swap; }
@font-face { font-family: "Outfit"; src: url(/fonts/Outfit-500-latin.woff2) format("woff2"); font-weight: 500; font-display: swap; }
@font-face { font-family: "Outfit"; src: url(/fonts/Outfit-600-latin.woff2) format("woff2"); font-weight: 600; font-display: swap; }
@font-face { font-family: "JetBrains Mono"; src: url(/fonts/JetBrainsMono-400-latin.woff2) format("woff2"); font-weight: 400; font-display: swap; }
:root {
  --gray-0: #03060a; --gray-1: #05080b; --gray-3: #151d27; --gray-4: #1f2936; --gray-5: #283541;
  --gray-6: #3a4a5c; --gray-7: #4b5f6c; --gray-9: #93a1ae; --gray-10: #9fb0bd; --gray-12: #e6edf3;
  --teal-fg: #2fe3c8; --teal-hover: #4debd4; --teal-press: #22c4ac; --on-teal: #04120f;
  --bad-fg: #ff7a8a; --bad-line: #61272f; --idle-fg: #aebeca;
  --text-1: var(--gray-12); --text-2: var(--gray-10); --text-3: var(--gray-9);
  --border-subtle: #2c3949; --border-default: var(--gray-6); --border-strong: var(--gray-7);
  --ease: cubic-bezier(0.2, 0.6, 0.35, 1); --ease-out: cubic-bezier(0.16, 0.84, 0.44, 1); --ease-expo: cubic-bezier(0.22, 1, 0.36, 1);
  --sans: "Outfit", system-ui, sans-serif; --mono: "JetBrains Mono", ui-monospace, monospace;
}
*, *::before, *::after { box-sizing: border-box; }
html { color-scheme: dark; -webkit-text-size-adjust: 100%; }
body {
  margin: 0; min-height: 100vh; min-height: 100dvh;
  display: grid; place-items: center; padding: 56px 24px;
  background: var(--gray-1); color: var(--text-1);
  font: 400 14px / 1.35 var(--sans); -webkit-font-smoothing: antialiased;
  isolation: isolate; overflow-x: hidden;
}
:focus-visible { outline: 2px solid var(--teal-fg); outline-offset: 2px; }
::selection { background: #0e2c2a; color: var(--text-1); }

/* blueprint grid lit by the pointer */
.ambient { position: fixed; inset: 0; z-index: -1; pointer-events: none; overflow: hidden; }
.ambient::before {
  content: ""; position: absolute; inset: 0;
  background: radial-gradient(ellipse 60% 42% at 50% 30%, color-mix(in oklab, var(--teal-fg) 7%, transparent), transparent 70%);
}
.ambient__grid, .ambient__lens { position: absolute; inset: 0; background-size: 24px 24px; background-position: 12px 12px; }
.ambient__grid {
  background-image: radial-gradient(circle at 1px 1px, var(--gray-6) 1px, transparent 1.5px);
  opacity: 0.45;
  -webkit-mask-image: radial-gradient(ellipse 80% 70% at 50% 40%, #000 0%, transparent 85%);
  mask-image: radial-gradient(ellipse 80% 70% at 50% 40%, #000 0%, transparent 85%);
}
.ambient__lens {
  background-image: radial-gradient(circle at 1px 1px, var(--teal-fg) 1.2px, transparent 1.8px);
  opacity: 0; transition: opacity 600ms var(--ease);
  -webkit-mask-image: radial-gradient(240px circle at var(--cx, -999px) var(--cy, -999px), #000, transparent 75%);
  mask-image: radial-gradient(240px circle at var(--cx, -999px) var(--cy, -999px), #000, transparent 75%);
}
html[data-pointer] .ambient__lens { opacity: 0.7; }

main { width: 100%; max-width: 420px; display: grid; gap: 28px; }

/* brand */
.brand { display: grid; justify-items: center; gap: 14px; text-align: center; }
.mark { overflow: visible; display: block; }
.mark__tok { fill: var(--text-1); transform-box: fill-box; }
.mark__tok--l { transform-origin: right center; }
.mark__tok--r { transform-origin: left center; }
.mark__tok--teal { fill: var(--teal-fg); }
.brand__name { font-size: 20px; font-weight: 400; color: var(--text-2); letter-spacing: 0.01em; line-height: 1.15; }
.brand__name b { font-weight: 600; color: var(--text-1); }
.brand__name small { display: block; margin-top: 4px; font-size: 12px; color: var(--text-3); letter-spacing: 0; }

/* verdict panel */
.verdict {
  position: relative; overflow: hidden;
  display: grid; gap: 24px;
  padding: 24px 24px 60px 27px;
  background:
    radial-gradient(ellipse 70% 120% at 0% 0%, color-mix(in oklab, var(--tone) 10%, transparent), transparent 70%),
    var(--gray-3);
  border: 1px solid var(--border-subtle); border-radius: 8px;
}
.verdict--idle { --tone: var(--idle-fg); }
.verdict--bad { --tone: var(--bad-fg); }
.verdict::before {
  content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
  background: var(--tone); transform-origin: top;
}
.verdict::after {
  content: ""; position: absolute; inset: 0; z-index: 2; padding: 1px;
  border-radius: inherit; pointer-events: none;
  background: radial-gradient(380px circle at var(--mx, -999px) var(--my, -999px), color-mix(in oklab, var(--teal-fg) 85%, transparent), transparent 65%);
  -webkit-mask: linear-gradient(rgb(0 0 0 / 0.92) 0 0) content-box, linear-gradient(#000 0 0);
  -webkit-mask-composite: xor;
  mask: linear-gradient(rgb(0 0 0 / 0.92) 0 0) content-box exclude, linear-gradient(#000 0 0);
  opacity: 0; transition: opacity 420ms var(--ease);
}
.verdict:hover::after, .verdict:focus-within::after { opacity: 1; }
.verdict__main { display: grid; gap: 8px; }
.eyebrow { font: 500 11px / 1 var(--sans); letter-spacing: 0.06em; text-transform: uppercase; color: var(--text-3); white-space: pre; }
.verdict--bad .eyebrow { color: var(--bad-fg); }
h1 { margin: 0; font-size: 28px; line-height: 1.25; font-weight: 600; letter-spacing: -0.01em; }
.detail { margin: 0; color: var(--text-2); font-size: 16px; line-height: 1.6; }

/* form */
form { position: relative; z-index: 3; display: grid; gap: 16px; }
.field { display: grid; gap: 8px; }
.field__label { font-size: 12px; font-weight: 500; color: var(--text-2); }
.field__box { position: relative; }
input {
  width: 100%; height: 42px; padding: 0 12px; border-radius: 4px;
  background: var(--gray-4); color: var(--text-1); border: 1px solid var(--border-subtle);
  font: 400 15px / 1 var(--sans); outline: none;
  transition: border-color 120ms var(--ease), background-color 120ms var(--ease), box-shadow 120ms var(--ease);
}
input:hover { border-color: var(--border-default); }
input:focus-visible { border-color: var(--teal-fg); box-shadow: 0 0 0 3px color-mix(in oklab, var(--teal-fg) 16%, transparent); }
.verdict--bad input#password { border-color: var(--bad-line); }
.verdict--bad input#password:focus-visible { border-color: var(--teal-fg); }
#password { padding-right: 64px; }
.reveal {
  position: absolute; right: 5px; top: 5px; height: 32px; padding: 0 10px;
  border: 0; border-radius: 4px; background: transparent; color: var(--text-3);
  font: 400 12px / 1 var(--mono); cursor: pointer;
  transition: color 120ms var(--ease), background-color 120ms var(--ease);
}
.reveal:hover { color: var(--text-1); background: var(--gray-5); }
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  height: 42px; margin-top: 4px; padding: 0 16px;
  font: 600 14px / 1 var(--sans); border-radius: 4px; border: 1px solid transparent; cursor: pointer;
  background: var(--teal-fg); color: var(--on-teal);
  transition: background-color 120ms var(--ease), transform 160ms var(--ease-expo), opacity 120ms;
}
.btn:hover:not(:disabled) { background: var(--teal-hover); }
.btn:active:not(:disabled) { transform: scale(0.97); background: var(--teal-press); }
.btn:disabled { cursor: progress; opacity: 0.6; }
.btn kbd { font: 400 11px / 1 var(--mono); opacity: 0.6; }

/* pulse along the bottom of the verdict */
.pulse { position: absolute; left: 3px; bottom: 6px; height: 34px; width: calc(100% - 3px); pointer-events: none; overflow: visible; }
.pulse path { fill: none; stroke: var(--tone); stroke-linejoin: round; stroke-linecap: round; vector-effect: non-scaling-stroke; }
.pulse__base { stroke-width: 1; opacity: 0.16; stroke-dasharray: 1; }
.pulse__trace { stroke-width: 1.6; filter: drop-shadow(0 0 4px var(--tone)); }

/* start-up log under the panel */
.log { display: grid; gap: 12px; justify-items: start; padding-left: 3px; }
.log ol { margin: 0; padding: 0; list-style: none; display: grid; gap: 2px; font: 400 12px / 1.6 var(--mono); color: var(--text-3); }
.log li { width: calc(var(--len) * 1ch + 1px); max-width: 100%; overflow: hidden; white-space: nowrap; }
.log li:last-child { color: var(--teal-fg); }
.log--bad li:last-child { color: var(--bad-fg); }
.meter { display: flex; gap: 3px; }
.meter span { width: 12px; height: 4px; background: var(--gray-5); }
.meter span.on { background: var(--teal-fg); box-shadow: 0 0 8px color-mix(in oklab, var(--teal-fg) 60%, transparent); }
.meter span.wait { background: var(--gray-6); }
.log--bad .meter span.wait { background: var(--bad-fg); box-shadow: 0 0 8px color-mix(in oklab, var(--bad-fg) 55%, transparent); }
html[data-going] .meter span { background: var(--teal-fg); box-shadow: 0 0 8px color-mix(in oklab, var(--teal-fg) 60%, transparent); }

@media (max-width: 480px) {
  body { padding: 40px 16px; place-items: start center; }
  main { gap: 24px; }
  .verdict { padding: 20px 20px 56px 23px; }
  h1 { font-size: 24px; }
  .detail { font-size: 15px; }
  .meter span { width: 10px; }
}

@media (prefers-reduced-motion: no-preference) {
  .mark__tok { animation: tok-grow 560ms var(--ease-expo) backwards; animation-delay: calc(var(--row) * 70ms + 60ms); }
  .mark__tok--teal {
    animation: tok-grow 560ms var(--ease-expo) backwards, tok-signal 1100ms var(--ease) backwards;
    animation-delay: calc(var(--row) * 70ms + 60ms), calc(var(--k) * 110ms + 620ms);
  }
  .brand:hover .mark__tok { animation: tok-think 720ms var(--ease-expo); animation-delay: calc(var(--row) * 40ms); }
  .brand:hover .mark__tok--teal {
    animation: tok-think 720ms var(--ease-expo), tok-signal 900ms var(--ease);
    animation-delay: calc(var(--row) * 40ms), calc(var(--k) * 90ms + 120ms);
  }
  .brand__name { animation: fade-up 520ms var(--ease-out) 420ms backwards; }
  .verdict { animation: rise 520ms var(--ease-expo) 220ms backwards; }
  .verdict::before { animation: spine-draw 800ms var(--ease-expo) 520ms backwards; }
  .verdict--bad { animation: rise 520ms var(--ease-expo) 120ms backwards, shake 420ms var(--ease) 640ms; }
  h1 { animation: fade-up 520ms var(--ease-out) 420ms backwards; }
  .pulse__base { animation: draw 1600ms var(--ease-expo) 700ms backwards; }
  .pulse__sweep { animation: pulse-sweep 5s linear 1100ms infinite backwards; }
  .verdict--bad .pulse__sweep { animation-duration: 2.2s; }
  .log li { animation: type calc(var(--len) * 11ms) steps(var(--len), end) both; animation-delay: calc(var(--l) * 230ms + 700ms); }
  .meter span { animation: fade-in 1ms linear calc(var(--i) * 55ms + 760ms) backwards; }
  .meter span.on { animation: brick 160ms var(--ease) backwards; animation-delay: calc(var(--i) * 55ms + 760ms); }
  .meter span.wait { animation: blink 1.2s steps(2, jump-none) calc(${LIT} * 55ms + 900ms) infinite; }
  html[data-going] .meter span { animation: brick 160ms var(--ease) backwards; animation-delay: calc((var(--i) - ${LIT}) * 30ms); }
}
@keyframes tok-grow { from { transform: scaleX(0); opacity: 0; } }
@keyframes tok-think { 40% { transform: scaleX(0.35); } }
@keyframes tok-signal { 0%, 100% { fill: var(--teal-fg); } 30% { fill: #fff; filter: drop-shadow(0 0 3px var(--teal-fg)); } }
@keyframes rise { from { opacity: 0; transform: translateY(10px); } }
@keyframes fade-up { from { opacity: 0; transform: translateY(4px); } }
@keyframes fade-in { from { opacity: 0; } }
@keyframes spine-draw { from { transform: scaleY(0); } }
@keyframes draw { from { stroke-dashoffset: 1; } to { stroke-dashoffset: 0; } }
@keyframes pulse-sweep { from { transform: translateX(0); } to { transform: translateX(1560px); } }
@keyframes type { from { width: 0; } }
@keyframes brick { from { background: var(--gray-5); box-shadow: none; } }
@keyframes blink { 50% { opacity: 0.25; } }
@keyframes shake { 20%, 60% { transform: translateX(-4px); } 40%, 80% { transform: translateX(4px); } }
</style>
</head>
<body>
<div class="ambient" aria-hidden="true"><div class="ambient__grid"></div><div class="ambient__lens"></div></div>
<main>
  <div class="brand">
    <svg class="mark" width="72" height="72" viewBox="0 0 64 64" aria-hidden="true">${MARK}</svg>
    <p class="brand__name" style="margin:0">Project <b>Brain</b><small>memory for your AI agents</small></p>
  </div>

  <section class="verdict verdict--${tone}" aria-labelledby="verdict">
    <div class="verdict__main">
      <p class="eyebrow" style="margin:0" data-decode>${failed ? "Access denied" : "Locked"}</p>
      <h1 id="verdict"${failed ? ' role="alert"' : ""}>${failed ? "That didn’t match." : "This Brain is private."}</h1>
      <p class="detail">${failed ? "Check the username and password and try again." : "Sign in to see what your agents know about your code."}</p>
    </div>
    <form method="post" action="${escape(action)}" id="login">
      <input type="hidden" name="next" value="${escape(next)}">
      <label class="field"><span class="field__label">Username</span>
        <input id="username" name="username" value="${escape(user)}" autocomplete="username" autocapitalize="none" spellcheck="false" required${user ? "" : " autofocus"}>
      </label>
      <label class="field"><span class="field__label">Password</span>
        <span class="field__box">
          <input id="password" name="password" type="password" autocomplete="current-password" required${user ? " autofocus" : ""}>
          <button class="reveal" type="button" aria-controls="password" aria-pressed="false">show</button>
        </span>
      </label>
      <button class="btn" type="submit">Sign in</button>
    </form>
    <svg class="pulse" viewBox="0 0 1200 40" preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <linearGradient id="pulse-g"><stop offset="0" stop-color="#000"/><stop offset="0.85" stop-color="#fff"/><stop offset="1" stop-color="#000"/></linearGradient>
        <mask id="pulse-m" maskUnits="userSpaceOnUse" x="0" y="0" width="1200" height="40">
          <rect class="pulse__sweep" x="-360" y="0" width="360" height="40" fill="url(#pulse-g)"/>
        </mask>
      </defs>
      <path class="pulse__base" d="${d}" pathLength="1"/>
      <path class="pulse__trace" d="${d}" mask="url(#pulse-m)"/>
    </svg>
  </section>

  <div class="log${failed ? " log--bad" : ""}" aria-hidden="true">
    <ol>${log.map((l, i) => `<li style="--l:${i};--len:${l.length}">${l}</li>`).join("")}</ol>
    <div class="meter">${Array.from({ length: BRICKS }, (_, i) => `<span class="${i < LIT ? "on" : i === LIT ? "wait" : ""}" style="--i:${i}"></span>`).join("")}</div>
  </div>
</main>
<script>
(function () {
  var root = document.documentElement;
  var panel = document.querySelector(".verdict");
  addEventListener("pointermove", function (e) {
    if (e.pointerType === "touch") return;
    root.style.setProperty("--cx", e.clientX + "px");
    root.style.setProperty("--cy", e.clientY + "px");
    root.setAttribute("data-pointer", "");
    var r = panel.getBoundingClientRect();
    panel.style.setProperty("--mx", e.clientX - r.left + "px");
    panel.style.setProperty("--my", e.clientY - r.top + "px");
  }, { passive: true });
  document.addEventListener("pointerleave", function () { root.removeAttribute("data-pointer"); });

  var pw = document.getElementById("password");
  var reveal = document.querySelector(".reveal");
  reveal.addEventListener("click", function () {
    var show = pw.type === "password";
    pw.type = show ? "text" : "password";
    reveal.textContent = show ? "hide" : "show";
    reveal.setAttribute("aria-pressed", String(show));
    pw.focus();
  });
  document.getElementById("login").addEventListener("submit", function (e) {
    var b = e.target.querySelector(".btn");
    b.disabled = true; b.textContent = "Waking up…";
    root.setAttribute("data-going", "");
  });

  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  var GLYPHS = "abcdefghijklmnopqrstuvwxyz0123456789/<>#_";
  document.querySelectorAll("[data-decode]").forEach(function (el) {
    var text = el.textContent, span = 380 + text.length * 22, start = 0;
    function tick(now) {
      start = start || now;
      var t = (now - start) / span;
      if (t >= 1) { el.textContent = text; return; }
      var fixed = Math.floor(t * text.length), out = "";
      for (var i = 0; i < text.length; i++) out += i < fixed || text[i] === " " ? text[i] : GLYPHS[(Math.random() * GLYPHS.length) | 0];
      el.textContent = out;
      requestAnimationFrame(tick);
    }
    setTimeout(function () { requestAnimationFrame(tick); }, 380);
  });
})();
</script>
</body>
</html>`;
}
