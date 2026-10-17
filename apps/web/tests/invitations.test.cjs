// Team invitations in the dashboard: the public /invite page (proxy.ts), the
// accept call it makes, the admin actions and the Team page's invitation list.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript5');
const root = path.resolve(__dirname, '..');

// Same harness as regressions.test.cjs: compile the real modules, isolate only the boundaries.
function load(file, overrides = {}) {
  const filename = path.join(root, file);
  const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const module = { exports: {} };
  const requireModule = (name) => {
    if (Object.hasOwn(overrides, name)) return overrides[name];
    if (name === 'server-only') return {};
    if (name.startsWith('./') || name.startsWith('../')) return load(path.relative(root, path.resolve(path.dirname(filename), `${name}.ts`)), overrides);
    return require(name);
  };
  vm.runInNewContext(output, { module, exports: module.exports, require: requireModule,
    process, Buffer, Response, URL, URLSearchParams, AbortSignal, console, performance, fetch: (...args) => global.fetch(...args) }, { filename });
  return module.exports;
}

const ENV = ['WEB_BASIC_AUTH', 'BRAIN_API_URL', 'BRAIN_API_KEY', 'BRAIN_DEMO_MODE', 'WEB_OPEN_MODE', 'WEB_LOOPBACK_BIND', 'BRAIN_WEB_ACTIONS'];
async function withEnv(values, run) {
  const previous = Object.fromEntries(ENV.map(name => [name, process.env[name]]));
  for (const name of ENV) values[name] === undefined ? delete process.env[name] : process.env[name] = values[name];
  try { return await run(); } finally { ENV.forEach(name => previous[name] === undefined ? delete process.env[name] : process.env[name] = previous[name]); }
}

async function withFetch(fake, run) {
  const previous = global.fetch;
  global.fetch = fake;
  try { return await run(); } finally { global.fetch = previous; }
}

const LIVE = { BRAIN_API_URL: 'https://api.example/brain', BRAIN_API_KEY: 'fixture' };
const TOKEN = `pbi_${'a'.repeat(43)}`;

// A Brain with one admin (alice) and invitations that mint member keys when accepted.
function fakeBrain({ team = true } = {}) {
  const brain = {
    team,
    members: { pbk_alice: { id: 7, name: 'alice', admin: true } },
    invitations: { [TOKEN]: { id: 9, name: 'carol', admin: false, key: 'pbk_carol_secret_key', accepted: false } },
    calls: [],
    accept: null, // force an accept answer: { status, detail } or 'down'
  };
  brain.fetch = async (url, options = {}) => {
    const u = new URL(String(url));
    const key = options.headers?.['X-API-Key'];
    brain.calls.push({ method: options.method ?? 'GET', path: u.pathname, key, body: options.body });
    if (u.pathname === '/brain/team/invitations/accept') {
      if (brain.accept === 'down') throw new TypeError('fetch failed');
      if (brain.accept) return Response.json({ detail: brain.accept.detail }, { status: brain.accept.status });
      const { token } = JSON.parse(options.body);
      const inv = brain.invitations[token];
      if (!inv) return Response.json({ detail: 'Invitation token is invalid, expired or revoked' }, { status: 403 });
      const first = !inv.accepted;
      inv.accepted = true;
      if (first) {
        brain.members[inv.key] = { id: inv.id, name: inv.name, admin: inv.admin };
        brain.team = true;
      }
      return Response.json({
        invitation: { id: 'b9a4c1de-0000-4000-8000-000000000001', state: 'verified', principal_name: inv.name, is_admin: inv.admin },
        member: { principal_id: inv.id, principal_name: inv.name, is_admin: inv.admin },
        project_memberships: [{ project_id: 'p1', role: 'contributor' }],
        credential: first ? inv.key : null,
      });
    }
    if (u.pathname.endsWith('/api/web-login')) {
      return brain.team
        ? Response.json({ detail: 'Named credential required: this Brain has team members' }, { status: 403 })
        : Response.json({ mode: 'password', user: 'owner', version: '1' });
    }
    if (u.pathname.endsWith('/api/web-session')) {
      const member = brain.members[key];
      if (!brain.team) return Response.json({ mode: 'solo', principal: { id: 0, name: 'owner', kind: 'service' }, team_admin: true, member: null });
      if (!member) return Response.json({ detail: 'Dashboard sign-in denied: credential revoked' }, { status: 403 });
      return Response.json({ mode: 'team', principal: { id: member.id, name: member.name, kind: 'human' }, team_admin: member.admin });
    }
    return Response.json({ detail: 'unexpected call' }, { status: 500 });
  };
  return brain;
}

function inviteProxy() {
  const { NextRequest } = require('next/server');
  const auth = load('lib/web-auth.ts');
  const { proxy } = load('proxy.ts', { './lib/web-auth': auth, '@/components/Icon': { MARK_TOKENS: [] } });
  const post = (fields, { origin = 'https://web.example', cookie, site } = {}) => new NextRequest('https://web.example/invite', {
    method: 'POST',
    body: new URLSearchParams(fields),
    headers: {
      'content-type': 'application/x-www-form-urlencoded', accept: 'text/html',
      ...(origin ? { origin } : {}), ...(cookie ? { cookie } : {}), ...(site ? { 'sec-fetch-site': site } : {}),
    },
  });
  const get = () => new NextRequest('https://web.example/invite', { headers: { accept: 'text/html' } });
  return { auth, proxy, post, get };
}

const accepts = brain => brain.calls.filter(c => c.path.endsWith('/team/invitations/accept'));
const count = (haystack, needle) => haystack.split(needle).length - 1;

test('invitation links carry the token in the fragment and parse back strictly', () => {
  const inv = load('lib/invitations.ts');
  assert.equal(inv.inviteLink('https://web.example/', TOKEN), `https://web.example/invite#token=${TOKEN}`);
  assert.equal(inv.tokenFromFragment(`#token=${TOKEN}`), TOKEN);
  assert.equal(inv.tokenFromFragment(`#${TOKEN}`), TOKEN);
  assert.equal(inv.tokenFromFragment(`#x=1&token=${TOKEN}`), TOKEN);
  for (const bad of ['', '#', '#token=', '#token=pbk_abcdefghij', '#token=pbi_short', '#token=%E0%A4%A', `#token=pbi_${'a'.repeat(125)}`, '#token=pbi_<script>xx']) {
    assert.equal(inv.tokenFromFragment(bad), null, bad);
  }
  assert.equal(inv.looksLikeToken(` ${TOKEN} `), true);
  assert.equal(inv.revokesMember('verified'), true);
  assert.equal(inv.revokesMember('active'), true);
  assert.equal(inv.revokesMember('invited'), false);
  assert.equal(inv.revokesMember('expired'), false);
});

test('accept outcomes: success shows the key, a retry never does, refusals stay uniform, outages are not verdicts', () => {
  const { acceptOutcome } = load('lib/invitations.ts');
  const ok = acceptOutcome(200, { member: { principal_name: 'carol', is_admin: true }, project_memberships: [{ project_id: 'p', role: 'reader' }, {}], credential: 'pbk_x' });
  assert.equal(ok.kind, 'accepted');
  assert.equal(ok.key, 'pbk_x');
  assert.equal(ok.admin, true);
  assert.deepEqual(ok.roles.map(r => ({ ...r })), [{ projectId: 'p', role: 'reader' }]);
  assert.deepEqual({ ...acceptOutcome(200, { member: { principal_name: 'carol' }, credential: null }) }, { kind: 'already', name: 'carol' });
  assert.equal(acceptOutcome(403, { detail: 'Invitation token is invalid, expired or revoked' }).kind, 'rejected');
  assert.equal(acceptOutcome(429, {}).kind, 'throttled');
  assert.deepEqual({ ...acceptOutcome(409, { detail: 'Principal name taken' }) }, { kind: 'conflict', message: 'Principal name taken' });
  assert.equal(acceptOutcome(404, { detail: 'Team is not initialized' }).kind, 'conflict');
  for (const [status, body] of [[500, {}], [502, null], [200, null], [200, { credential: 'pbk_x' }], [409, {}], [0, null]]) {
    assert.equal(acceptOutcome(status, body).kind, 'unavailable', `${status} ${JSON.stringify(body)}`);
  }
});

test('the accept call sends the token in the body and no API key, keeps the path prefix, and skips malformed tokens', async () => {
  const brain = fakeBrain();
  await withEnv(LIVE, () => withFetch(brain.fetch, async () => {
    const { acceptInvitation } = load('lib/invite-accept.ts');
    assert.equal((await acceptInvitation('not-a-token')).kind, 'malformed');
    assert.equal(brain.calls.length, 0, 'a malformed token never reaches Brain');
    const res = await acceptInvitation(`  ${TOKEN}\n`);
    assert.equal(res.kind, 'accepted');
    const [call] = accepts(brain);
    assert.equal(call.method, 'POST');
    assert.equal(call.key, undefined, 'the server key must not vouch for an anonymous invitee');
    assert.deepEqual(JSON.parse(call.body), { token: TOKEN });
    brain.accept = 'down';
    assert.equal((await acceptInvitation(TOKEN)).kind, 'unavailable');
  }));
  await withEnv({}, async () => assert.equal((await load('lib/invite-accept.ts').acceptInvitation(TOKEN)).kind, 'unavailable'));
});

test('/invite is public, shows the token form and never reaches Brain on a GET', async () => {
  const brain = fakeBrain();
  await withEnv(LIVE, () => withFetch(brain.fetch, async () => {
    const { proxy, get } = inviteProxy();
    const res = await proxy(get());
    assert.equal(res.status, 200);
    assert.equal(res.headers.get('cache-control'), 'no-store');
    assert.equal(res.headers.get('x-frame-options'), 'DENY');
    assert.equal(res.headers.get('referrer-policy'), 'no-referrer');
    const html = await res.text();
    assert.match(html, /<textarea id="token" name="token"/);
    assert.match(html, /Join the team · Project Brain/);
    assert.match(html, /history\.replaceState/, 'the token is cleared from the address bar');
    assert.doesNotMatch(html, /fixture/);
    assert.ok(brain.calls.every(c => c.path.endsWith('/api/web-login')), 'only the sign-in state probe');
  }));
});

test('accepting signs the invitee in with a sealed cookie and shows the key exactly once; a reload does not show it again', async () => {
  const brain = fakeBrain();
  await withEnv(LIVE, () => withFetch(brain.fetch, async () => {
    const { auth, proxy, post } = inviteProxy();
    const res = await proxy(post({ token: TOKEN }));
    assert.equal(res.status, 200);
    const html = await res.text();
    assert.match(html, /You’re in, carol\./);
    assert.equal(count(html, 'pbk_carol_secret_key'), 1, 'the key appears once, in the key box');
    assert.match(html, /data-copy="member-key"/);
    const setCookie = res.headers.get('set-cookie');
    assert.match(setCookie, /pb_member=[^;]+/);
    assert.match(setCookie, /HttpOnly/i);
    assert.match(setCookie, /Secure/i);
    assert.match(setCookie, /SameSite=lax/i);
    assert.doesNotMatch(setCookie, /pbk_carol/);
    const sealed = decodeURIComponent(/pb_member=([^;]+)/.exec(setCookie)[1]);
    assert.equal(auth.openMember(sealed), 'pbk_carol_secret_key');
    assert.ok(brain.calls.some(c => c.path.endsWith('/api/web-session') && c.key === 'pbk_carol_secret_key'), 'signed in the same way as the member-key form');

    // The browser resubmits the form: same member, no key.
    const again = await proxy(post({ token: TOKEN }, { cookie: `pb_member=${sealed}` }));
    assert.equal(again.status, 200);
    const againHtml = await again.text();
    assert.match(againHtml, /already yours/);
    assert.doesNotMatch(againHtml, /pbk_carol/);
    // Someone else with the used link.
    const other = await proxy(post({ token: TOKEN }));
    assert.equal(other.status, 409);
    const otherHtml = await other.text();
    assert.match(otherHtml, /already accepted/);
    assert.doesNotMatch(otherHtml, /pbk_carol/);
    assert.equal(other.headers.get('set-cookie'), null);
  }));
});

test('a solo Brain becomes a team through its first accepted invitation and the invitee is signed in', async () => {
  const brain = fakeBrain({ team: false });
  await withEnv({ ...LIVE, WEB_BASIC_AUTH: undefined }, () => withFetch(brain.fetch, async () => {
    const { auth, proxy, post } = inviteProxy();
    assert.equal((await auth.loginState()).kind, 'brain');
    const res = await proxy(post({ token: TOKEN }));
    assert.equal(res.status, 200);
    assert.match(await res.text(), /You’re in, carol\./);
    assert.match(res.headers.get('set-cookie'), /pb_member=/);
    assert.equal((await auth.loginState()).kind, 'team', 'the cached solo answer was dropped');
  }));
});

test('refusals: wrong/expired/revoked, throttled, conflict and an outage each say so, without a cookie', async () => {
  const brain = fakeBrain();
  await withEnv(LIVE, () => withFetch(brain.fetch, async () => {
    const { proxy, post } = inviteProxy();
    const unknown = await proxy(post({ token: `pbi_${'z'.repeat(43)}` }));
    assert.equal(unknown.status, 403);
    const unknownHtml = await unknown.text();
    assert.match(unknownHtml, /wrong, has expired or was revoked/);
    assert.doesNotMatch(unknownHtml, /zzzzzzzz/, 'a refused token is not echoed back');

    brain.accept = { status: 429, detail: 'Too many invitation attempts; try again later' };
    const throttled = await proxy(post({ token: TOKEN }));
    assert.equal(throttled.status, 429);
    assert.match(await throttled.text(), /Too many attempts/);

    brain.accept = { status: 409, detail: 'Principal name \'<b>carol</b>\' is already taken; ask for a new invitation' };
    const conflict = await proxy(post({ token: TOKEN }));
    assert.equal(conflict.status, 409);
    const conflictHtml = await conflict.text();
    assert.match(conflictHtml, /&lt;b&gt;carol&lt;\/b&gt;/, 'Brain detail is escaped');
    assert.doesNotMatch(conflictHtml, /<b>carol<\/b>/);

    brain.accept = 'down';
    const down = await proxy(post({ token: TOKEN }));
    assert.equal(down.status, 503);
    const downHtml = await down.text();
    assert.match(downHtml, /Brain isn’t answering/);
    assert.match(downHtml, /may or may not have accepted/);
    assert.doesNotMatch(downHtml, /invitation wasn’t used/);

    const malformed = await proxy(post({ token: 'hello' }));
    assert.equal(malformed.status, 400);
    for (const r of [unknown, throttled, conflict, down, malformed]) assert.equal(r.headers.get('set-cookie'), null);
  }));
});

test('a cross-site POST to /invite is refused before Brain is asked (login CSRF)', async () => {
  const brain = fakeBrain();
  await withEnv(LIVE, () => withFetch(brain.fetch, async () => {
    const { proxy, post } = inviteProxy();
    for (const req of [post({ token: TOKEN }, { origin: 'https://evil.example' }), post({ token: TOKEN }, { origin: 'http://web.example' }), post({ token: TOKEN }, { origin: null }), post({ token: TOKEN }, { origin: null, site: 'cross-site' }), post({ token: TOKEN }, { origin: 'null' })]) {
      const res = await proxy(req);
      assert.equal(res.status, 403);
      assert.equal(res.headers.get('set-cookie'), null);
    }
    assert.equal(accepts(brain).length, 0);
    // Same-origin without an Origin header (older browsers send Sec-Fetch-Site instead).
    assert.equal((await proxy(post({ token: TOKEN }, { origin: null, site: 'same-origin' }))).status, 200);
  }));
});

test('the Team page pauses invitation creation when projects are unavailable', () => {
  const page = fs.readFileSync(path.join(root, 'app/team/page.tsx'), 'utf8');
  const form = fs.readFileSync(path.join(root, 'components/team/Invitations.tsx'), 'utf8');
  assert.match(page, /projectsAvailable=\{view\.kind === "ok"\}/);
  assert.match(form, /invitation creation is paused/);
  assert.match(form, /disabled=\{!projectsAvailable \|\| pending \|\| mode !== "on"\}/);
});

test('/invite explains itself on demo data, when misconfigured and when dashboard changes are switched off', async () => {
  const brain = fakeBrain();
  await withFetch(brain.fetch, async () => {
    await withEnv({}, async () => {
      const { proxy, post } = inviteProxy();
      const res = await proxy(post({ token: TOKEN }));
      assert.equal(res.status, 503);
      assert.match(await res.text(), /Invitations need a live Brain/);
    });
    await withEnv({ BRAIN_API_URL: 'https://api.example/brain' }, async () => {
      const { proxy, get } = inviteProxy();
      const res = await proxy(get());
      assert.equal(res.status, 503);
      assert.match(await res.text(), /misconfigured/);
    });
    await withEnv({ ...LIVE, BRAIN_WEB_ACTIONS: '0' }, async () => {
      const { proxy, post } = inviteProxy();
      const res = await proxy(post({ token: TOKEN }));
      assert.equal(res.status, 503);
      assert.match(await res.text(), /BRAIN_WEB_ACTIONS=0/);
    });
  });
  assert.equal(accepts(brain).length, 0);
});

test('the sign-in page still renders through the shared document shell', () => {
  const { loginPage } = load('lib/login-page.ts', { '@/components/Icon': { MARK_TOKENS: [] } });
  const html = loginPage({ action: '/__login', next: '/team', failed: false });
  assert.match(html, /<title>Sign in · Project Brain<\/title>/);
  assert.match(html, /name="username"/);
  assert.match(html, /verdict--idle/);
  assert.doesNotMatch(html, /<script>\s*<\/script>/, 'no empty extra script');
  const refused = loginPage({ action: '/__login', next: '/', failed: true, team: true });
  assert.match(refused, /role="alert"/);
  assert.match(refused, /log--bad/);
  assert.match(refused, /name="credential"/);
});

test('the invite page keeps a slot for other ways to join (e.g. single sign-on)', () => {
  const { invitePage } = load('lib/invite-page.ts', { '@/components/Icon': { MARK_TOKENS: [] } });
  const { html } = invitePage({ alternatives: [{ label: 'Continue with <SSO>', href: '/auth/oidc/start?x="1"' }] });
  assert.match(html, /Continue with &lt;SSO&gt;/);
  assert.match(html, /href="\/auth\/oidc\/start\?x=&quot;1&quot;"/);
  assert.doesNotMatch(invitePage().html, /class="alt"/);
});

test('invitation list: denied, unavailable and empty are three different answers', async () => {
  let answer;
  const view = load('lib/invitation-view.ts', { './api': { brainRead: async (p, opts) => {
    assert.equal(p, '/team/invitations');
    assert.equal(opts.fresh, true);
    return answer;
  } } });
  answer = { ok: true, status: 200, data: [] };
  assert.deepEqual((await view.getInvitationsView()).kind, 'ok');
  assert.equal((await view.getInvitationsView()).invitations.length, 0);
  answer = { ok: false, status: 403, reason: 'http_error' };
  assert.equal((await view.getInvitationsView()).kind, 'denied');
  answer = { ok: false, status: 401, reason: 'http_error' };
  assert.equal((await view.getInvitationsView()).kind, 'denied');
  answer = { ok: false, status: 0, reason: 'network' };
  assert.equal((await view.getInvitationsView()).kind, 'unavailable');
  answer = { ok: false, status: 500, reason: 'http_error' };
  assert.equal((await view.getInvitationsView()).kind, 'unavailable');
  answer = { ok: true, status: 200, data: { detail: 'not a list' } };
  assert.equal((await view.getInvitationsView()).kind, 'unavailable');
  answer = { ok: true, status: 200, data: [
    { id: 'a', state: 'revoked', principal_name: 'old', project_ids: [], role: 'reader', created_at: '2026-10-01T00:00:00+00:00' },
    { id: 'b', state: 'invited', principal_name: 'new', email: 'n@example.com', project_ids: ['p1', 7], role: 'reviewer', is_admin: true, created_at: '2026-10-02T00:00:00+00:00', expires_at: '2026-10-05T00:00:00+00:00' },
    { id: 'c', state: 'mystery' },
    { id: 'd', state: 'active', principal_name: 'carol', role: 'nonsense', accepted_principal_id: 9 },
  ] };
  const ok = await view.getInvitationsView();
  assert.deepEqual([...ok.invitations.map(r => r.id)], ['b', 'd', 'a']);
  assert.deepEqual([...ok.invitations[0].projectIds], ['p1']);
  assert.equal(ok.invitations[0].admin, true);
  assert.equal(ok.invitations[1].role, 'contributor');
  assert.equal(ok.invitations[1].memberId, 9);
});

test('invitation actions validate input before calling Brain and send the API’s shapes', async () => {
  const calls = [];
  const team = load('lib/actions/team.ts', { './gate': { mutate: async (p, opts) => {
    calls.push({ path: p, ...opts });
    if (p === '/team/invitations') return { ok: true, message: opts.success, data: { token: TOKEN, invitation: { id: 'b9a4c1de-0000-4000-8000-000000000001', principal_name: 'dana', expires_at: '2026-10-21T10:00:00+00:00' } } };
    return { ok: true, message: opts.success };
  } } });
  const project = '0f5e2c1a-1111-4222-8333-444455556666';
  const base = { name: 'dana', email: '', projects: [project], role: 'reader', admin: false, expiresHours: 72 };
  for (const bad of [{ name: 'dana smith' }, { name: '1dana' }, { email: 'not-an-email' }, { projects: ['core'] }, { projects: Array.from({ length: 17 }, (_, i) => `0f5e2c1a-1111-4222-8333-${String(i).padStart(12, '0')}`) }, { role: 'owner' }, { expiresHours: 0 }, { expiresHours: 721 }, { expiresHours: Number.NaN }]) {
    const res = await team.createInvitation({ ...base, ...bad });
    assert.equal(res.ok, false, JSON.stringify(bad));
  }
  assert.equal(calls.length, 0, 'nothing invalid reaches Brain');
  const made = await team.createInvitation({ ...base, name: ' dana ', projects: [project, project] });
  assert.equal(made.ok, true);
  assert.equal(made.data.token, TOKEN);
  assert.equal(made.data.name, 'dana');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)), { projects: [project], role: 'reader', is_admin: false, email: null, name: 'dana', expires_hours: 72 });
  const unnamed = await team.createInvitation({ ...base, name: '', projects: [] });
  assert.equal(unnamed.ok, true);
  assert.equal(calls[1].body.name, null, 'Brain picks a name');

  assert.equal((await team.revokeInvitation('../members', false)).ok, false);
  assert.equal(calls.length, 2);
  await team.revokeInvitation('b9a4c1de-0000-4000-8000-000000000001', true);
  assert.equal(calls[2].path, '/team/invitations/b9a4c1de-0000-4000-8000-000000000001/revoke');
  assert.deepEqual({ ...calls[2].body }, { revoke_member: true });
});
