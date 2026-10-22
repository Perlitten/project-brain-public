const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
// TypeScript 7 is the project's CLI compiler; this harness needs the legacy runtime API.
const ts = require('typescript5');
const root = path.resolve(__dirname, '..');

// Compile the actual TypeScript modules; isolate only Next request and API boundaries.
function load(file, overrides = {}) {
  const filename = path.join(root, file);
  const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const module = { exports: {} };
  const requireModule = (name) => {
    if (Object.hasOwn(overrides, name)) {
      const boundary = overrides[name];
      if (name === './api' && boundary.brainFetch && !boundary.brainRead) return {
        ...boundary,
        readFailureNote: load('lib/api.ts').readFailureNote,
        brainRead: async (...args) => {
          const data = await boundary.brainFetch(...args);
          return data === null ? { ok: false, status: 404, reason: 'not_found' } : { ok: true, status: 200, data };
        },
      };
      return boundary;
    }
    if (name === 'server-only') return {};
    if (name.startsWith('./') || name.startsWith('../')) return load(path.relative(root, path.resolve(path.dirname(filename), `${name}.ts`)), overrides);
    return require(name);
  };
  vm.runInNewContext(output, { module, exports: module.exports, require: requireModule,
    process, Buffer, Response, URL, URLSearchParams, AbortSignal, console, performance, fetch: (...args) => global.fetch(...args) }, { filename });
  return module.exports;
}

function withEnv(values, run) {
  const names = ['WEB_BASIC_AUTH', 'BRAIN_API_URL', 'BRAIN_API_KEY', 'BRAIN_DEMO_MODE', 'WEB_OPEN_MODE', 'WEB_LOOPBACK_BIND'];
  const previous = Object.fromEntries(names.map(name => [name, process.env[name]]));
  for (const name of names) {
    if (values[name] === undefined) delete process.env[name];
    else process.env[name] = values[name];
  }
  const restore = () => names.forEach(name => previous[name] === undefined ? delete process.env[name] : process.env[name] = previous[name]);
  try {
    const result = run();
    if (result?.then) return result.finally(restore);
    restore();
    return result;
  } catch (error) { restore(); throw error; }
}

test('setup preserves timeout and authorization failures, then recovers without a restart', async () => {
  let reply = { ok: false, status: 0, reason: 'timeout' };
  const view = load('lib/setup-view.ts', {
    'next/server': { connection: async () => {} },
    './api': { apiConfigured: true, brainRead: async () => reply, brainFetch: async () => null },
  });
  const timeout = await view.getSetupView();
  assert.equal(timeout.mode, 'down');
  assert.match(timeout.problem.title, /taking too long/);
  assert.equal(timeout.problem.retry, true);
  assert.doesNotMatch(timeout.problem.detail, /docker compose/);
  reply = { ok: false, status: 403, reason: 'http_error' };
  const denied = await view.getSetupView();
  assert.match(denied.problem.title, /Permission/);
  assert.equal(denied.problem.retry, false);
  reply = { ok: true, status: 200, data: { steps: [] } };
  assert.match((await view.getSetupView()).problem.title, /Unreadable/, 'an empty status cannot imply completed setup');
  reply = { ok: true, status: 200, data: { steps: [{ id: 'provider', status: 'done', detail: 'llm: verified at 2026-10-19T14:30:00+03:00' }] } };
  const recovered = await view.getSetupView();
  assert.equal(recovered.mode, 'live');
  assert.equal(recovered.steps[0].done, true);
  assert.match(recovered.steps[0].detail, /Language model: verified at .*11:30 UTC/);
});

test('read errors distinguish expired sign-in, permission, network and server errors', () => {
  const { readProblem } = load('lib/read-problem.ts');
  assert.match(readProblem({ ok: false, status: 401, reason: 'http_error' }).title, /Sign-in/);
  assert.match(readProblem({ ok: false, status: 403, reason: 'http_error' }).title, /Permission/);
  assert.match(readProblem({ ok: false, status: 0, reason: 'network' }).title, /Connection/);
  const unavailable = readProblem({ ok: false, status: 503, reason: 'http_error' });
  assert.match(unavailable.title, /503/);
  assert.equal(unavailable.retry, true);
});

test('a failed provider catalogue never masquerades as an old API or resets completed setup', async () => {
  const view = load('lib/setup-view.ts', {
    'next/server': { connection: async () => {} },
    './api': { apiConfigured: true, brainRead: async path => path.endsWith('/providers')
      ? { ok: false, status: 0, reason: 'timeout' }
      : { ok: true, status: 200, data: { steps: [{ id: 'provider', status: 'done' }] } } },
  });
  const result = await view.getSetupView();
  assert.equal(result.mode, 'live');
  assert.equal(result.steps[0].done, true);
  assert.equal(result.providers, null);
  assert.match(result.providersProblem.title, /taking too long/);
});

test('repository summaries never call a missing source or unverified freshness behind', () => {
  const { repositorySummary, repositoryStatus } = load('lib/repository-status.ts');
  const repos = [
    { freshness: 'source_missing', behind: 8 },
    { freshness: 'behind', behind: 2 },
    { freshness: 'freshness_error', behind: null },
    { freshness: 'current', behind: 0 },
  ];
  assert.match(repositoryStatus(repos[0]).status, /Source unavailable/);
  assert.equal(repositorySummary(repos), '4 projects: 1 source unavailable; 1 behind their code; 1 need a freshness check or indexing');
  assert.match(repositorySummary([repos[3]]), /up to date/);
});

test('tool health does not extrapolate one successful tool to unused tools, and flags slow responses', async () => {
  const { toolSummary } = load('lib/tool-health.ts');
  const data = load('lib/data.ts', {
    'next/server': { connection: async () => {} }, react: { cache: fn => fn },
    './api': { apiConfigured: true, brainFetch: async () => ({ available: true, tools: [
      { name: 'search_code', calls_24h: 1, error_rate: 0, p95_ms: 3300 },
      { name: 'prepare_task_context', calls_24h: 0, error_rate: 0 },
    ] }) },
  });
  const tools = await data.getMcpTools();
  assert.equal(tools[0].status, 'warn');
  assert.equal(tools[1].status, 'idle');
  assert.equal(toolSummary(tools).tone, 'warn');
  const oneFast = [{ ...tools[0], status: 'ok' }, tools[1]];
  assert.equal(toolSummary(oneFast).tone, 'info');
  assert.match(toolSummary(oneFast).text, /1 of 2.*remaining tools unverified/);
  assert.equal(toolSummary([{ ...tools[0], status: 'ok' }]).tone, 'ok');
});

test('access marks the request identity and key without guessing from an integration name', async () => {
  const data = load('lib/data.ts', {
    'next/server': { connection: async () => {} }, react: { cache: fn => fn },
    './api': { apiConfigured: true, brainFetch: async () => ({ current_principal_id: 3, current_credential_id: 11, principals: [
      { id: 3, name: 'any-name', kind: 'service', credentials: [{ id: 11, scopes: ['principals:write'] }, { id: 12, scopes: ['principals:read'] }] },
      { id: 4, name: 'web-ui-vercel', kind: 'service', credentials: [{ id: 13, scopes: ['core:read'] }] },
    ] }) },
  });
  const { identities } = await data.getAccessView();
  assert.equal(identities[0].current, true);
  assert.equal(identities[0].keys.find(k => k.id === 11).current, true);
  assert.equal(identities[0].keys.find(k => k.id === 12).current, false);
  assert.equal(identities[1].current, false);
});

test('key rotation preserves dotted team scopes and passes them to Brain for authorization', async () => {
  const requests = [];
  const admin = load('lib/actions/admin.ts', { './gate': { mutate: async (path, opts) => {
    requests.push({ path, ...opts });
    return { ok: true, message: '', data: { api_key: 'synthetic-member-key' } };
  } } });
  const rotated = await admin.mintKey(3, ['team:knowledge.propose', 'team:read'], 90);
  assert.equal(rotated.ok, true);
  assert.deepEqual([...requests[0].body.scopes], ['team:knowledge.propose', 'team:read']);
  assert.equal(requests[0].body.ttl_days, 90);
  for (const scope of ['*', 'team:.admin', 'team:admin.', 'team:knowledge..approve']) {
    assert.equal((await admin.mintKey(3, [scope], 90)).ok, false);
  }
  assert.equal(requests.length, 1, 'invalid scopes never reach the mutation boundary');
});

test('timestamps normalize offsets to UTC and never invent a timezone', () => {
  const { utcTime } = load('lib/time.ts');
  assert.equal(utcTime('2026-10-19T14:30:00+03:00'), utcTime('2026-10-19T11:30:00Z'));
  assert.match(utcTime('2026-10-19T11:30:00'), /timezone unknown/);
  assert.equal(utcTime('invalidZ'), 'unknown time');
});

test('team bootstrap saves a human key before activation, and never switches sign-in on refusal', async () => {
  const requests = [], cookies = [];
  let permitted = false, forgotten = 0;
  const bootstrap = load('lib/actions/bootstrap.ts', {
    'next/headers': { cookies: async () => ({ set: (...args) => cookies.push(args) }), headers: async () => new Map([['x-forwarded-proto', 'https']]) },
    'next/cache': { revalidatePath: () => {} },
    '../web-auth': { MEMBER_COOKIE: 'pb_member', MEMBER_MAX_AGE: 604800, sealMember: () => 'sealed-cookie', forgetLoginState: () => { forgotten++; } },
    './admin': { createAgentKey: async (name, scopes, opts) => {
      requests.push({ name, scopes, opts });
      return { ok: true, message: '', data: { apiKey: 'saved-human-key', name } };
    } },
    './gate': { mutate: async (path, opts) => {
      requests.push({ path, ...opts });
      return { ok: permitted, message: permitted ? 'Enabled' : 'Owner permission required' };
    } },
  });
  const prepared = await bootstrap.prepareFirstAdmin('alice');
  assert.equal(prepared.ok, true);
  assert.equal(requests[0].opts.kind, 'human');
  assert.equal(requests[0].opts.ttlDays, 90);
  assert.ok(requests[0].scopes.includes('team:admin'));
  assert.equal(requests.some(r => r.path), false, 'preparing never activates membership');
  assert.equal((await bootstrap.activateFirstAdmin({ teamName: 'Team', credential: prepared.data.apiKey, saved: false })).ok, false);
  assert.equal(requests.length, 1, 'acknowledgement is required before sending activation');
  const input = { teamName: 'Team', credential: prepared.data.apiKey, saved: true };
  assert.equal((await bootstrap.activateFirstAdmin(input)).ok, false);
  assert.equal(cookies.length, 0);
  assert.equal(forgotten, 0);
  permitted = true;
  const activated = await bootstrap.activateFirstAdmin(input);
  assert.equal(activated.ok, true);
  assert.doesNotMatch(JSON.stringify(activated), /saved-human-key/);
  assert.equal(cookies.length, 1);
  assert.equal(cookies[0][1], 'sealed-cookie');
  assert.equal(cookies[0][2].httpOnly, true);
  assert.equal(cookies[0][2].secure, true);
  assert.equal(cookies[0][2].sameSite, 'lax');
  assert.equal(forgotten, 1);
});

const basic = value => `Basic ${Buffer.from(value).toString('base64')}`;

test('overview failures cannot suppress another repository or a recovered request', async () => {
  let recovered = false;
  const calls = [];
  const data = load('lib/data.ts', {
    'next/server': { connection: async () => {} },
    react: { cache: fn => fn },
    './api': { apiConfigured: true, brainFetch: async path => {
      calls.push(path);
      if (path === '/repositories') return { repositories: [1, 2, 3, 4].map(id => ({ id, name: `repo${id}` })) };
      if (path.startsWith('/api/web/overview')) {
        if (!recovered && !path.endsWith('=4')) return null;
        return { counts: { files: 8, chunks: 10, symbols: 12, embeddings: 10 }, jobs: [] };
      }
      return null;
    } },
  });
  for (const id of [1, 2, 3]) assert.equal((await data.getCorpus(String(id))).available, false);
  assert.equal((await data.getCorpus('4')).files, 8);
  recovered = true;
  assert.equal((await data.getCorpus('1')).available, true);
  assert.ok(calls.includes('/api/web/overview?include_health=false&repository_id=4'));
});

test('repository feed distinguishes empty success, malformed payload and an outage', async () => {
  let response = { repositories: [] };
  const data = load('lib/data.ts', {
    'next/server': { connection: async () => {} }, react: { cache: fn => fn },
    './api': { apiConfigured: true, brainFetch: async () => response },
  });
  assert.equal((await data.getRepositoryFeed()).available, true);
  assert.equal((await data.getRepositoryFeed()).repositories.length, 0);
  response = null;
  assert.equal((await data.getRepositoryFeed()).available, false);
  response = { unexpected: true };
  const invalid = await data.getRepositoryFeed();
  assert.equal(invalid.available, false);
  assert.match(invalid.note, /unreadable/);
});

test('failed jobs collection is unavailable, while an empty successful collection stays empty', async () => {
  let failed = true;
  const data = load('lib/data.ts', {
    'next/server': { connection: async () => {} }, react: { cache: fn => fn },
    './api': { apiConfigured: true, brainFetch: async path => path === '/repositories'
      ? { repositories: [{ id: 1, name: 'repo' }] }
      : { jobs: [], jobs_error: failed ? 'collection timed out' : null } },
  });
  assert.equal(await data.getJobs('1'), null);
  failed = false;
  assert.equal((await data.getJobs('1')).length, 0);
});

test('API reads preserve missing, HTTP failure, invalid JSON and timeout reasons and recover immediately', async () => {
  await withEnv({ BRAIN_API_URL: 'http://127.0.0.1:9999', BRAIN_API_KEY: 'synthetic-server-key' }, async () => {
    const previous = global.fetch;
    try {
      global.fetch = async () => new Response('{}', { status: 404 });
      const api = load('lib/api.ts');
      assert.equal((await api.brainRead('/fixture')).reason, 'not_found');
      global.fetch = async () => new Response('{}', { status: 500 });
      assert.equal((await api.brainRead('/fixture')).reason, 'http_error');
      global.fetch = async () => new Response('invalid JSON');
      assert.equal((await api.brainRead('/fixture')).reason, 'invalid_response');
      global.fetch = async (url, options) => new Promise((resolve, reject) => {
        const keepAlive = setTimeout(() => reject(new Error('deadline was not enforced')), 200);
        options.signal.addEventListener('abort', () => { clearTimeout(keepAlive); reject(options.signal.reason); }, { once: true });
      });
      const hanging = load('lib/api.ts');
      assert.equal((await hanging.brainRead('/fixture', { timeoutMs: 20 })).reason, 'timeout');
      global.fetch = async () => new Response('{"results":[]}');
      const recovered = load('lib/api.ts');
      assert.equal((await recovered.brainRead('/fixture')).ok, true);
      assert.equal((await recovered.brainFetch('/fixture')).results.length, 0);
    } finally { global.fetch = previous; }
  });
});

test('API requests preserve the configured path prefix and reject encoded traversal', async () => {
  await withEnv({ BRAIN_API_URL: 'https://api.example/brain', BRAIN_API_KEY: 'synthetic-server-key' }, async () => {
    const previous = global.fetch;
    const urls = [];
    try {
      global.fetch = async url => {
        // The sign-in state probe (solo or team) is not part of what is asserted here.
        if (!String(url).endsWith('/api/web-login')) urls.push(String(url));
        return new Response('{}');
      };
      const api = load('lib/api.ts');
      assert.equal((await api.brainRead('/repositories?limit=1')).ok, true);
      assert.equal(urls[0], 'https://api.example/brain/repositories?limit=1');
      assert.equal((await api.brainRead('/%2e%2e/admin')).reason, 'not_configured');
      assert.equal((await api.brainSend('POST', '/%2E%2E/admin')).ok, false);
      assert.equal(urls.length, 1, 'rejected paths must not reach fetch');
    } finally { global.fetch = previous; }
  });
});

test('demo history search and facets filter before paging without calling a backend', async () => {
  const overrides = {
    'next/server': { connection: async () => {} },
    react: { cache: fn => fn },
    './api': { apiConfigured: false, brainFetch: async () => { throw new Error('demo must not call a backend'); } },
  };
  const data = load('lib/data.ts', overrides);
  for (const result of await Promise.all([
    data.getPagedAgentRuns(undefined, { q: 'no-matching-example' }),
    data.getPagedIndexRuns(undefined, { q: 'no-matching-example' }),
    data.getPagedContextPacks({ q: 'no-matching-example' }),
    data.getPagedReports({ q: 'no-matching-example' }),
    data.getPagedInsights(undefined, { q: 'no-matching-example' }),
    data.getPagedDecisions(undefined, { q: 'no-matching-example' }),
    data.getPagedRules(undefined, { q: 'no-matching-example' }),
  ])) {
    assert.equal(result.items.length, 0);
    assert.equal(result.paging.total, 0);
  }
  const packs = await data.getPagedContextPacks({ status: 'outdated' }, 'billing-service');
  assert.equal(packs.items.length, 2);
  assert.ok(packs.items.every(pack => pack.repo === 'billing-service' && pack.stale));
  assert.equal(packs.paging.facets.fresh, 1);
  assert.equal((await data.getPagedIndexRuns('missing')).items.length, 0);

  const decisions = Array.from({ length: 45 }, (_, i) => ({
    id: `D-${i}`, title: i < 35 ? 'Matching example' : 'Other entry',
    summary: '', scope: 'repo', repo: 'repo', status: i < 30 ? 'accepted' : 'proposed',
  }));
  const fixture = load('lib/data.ts', { ...overrides, './mock': { decisions } });
  const page = await fixture.getPagedDecisions('repo', { q: 'MATCHING example', status: 'accepted', page: 2 });
  assert.equal(page.items.length, 10);
  assert.equal(page.items[0].id, 'D-20');
  assert.equal(page.paging.total, 30);
  assert.equal(page.paging.facets.proposed, 5);
  assert.equal((await fixture.getPagedDecisions('missing')).paging.total, 0);
});

test('scheduler degradation is a warning rather than an empty service outage', () => {
  const { pulseHeadline } = load('lib/pulse-presentation.ts');
  assert.equal(pulseHeadline({ reachable: true, healthy: false, down: [] }), 'Needs attention');
  assert.equal(pulseHeadline({ reachable: true, healthy: false, down: ['postgres'] }), 'postgres down');
  assert.equal(pulseHeadline({ reachable: false, down: [] }), 'Not answering');
  assert.equal(pulseHeadline({ reachable: true, healthy: true, down: [] }), 'Healthy');
});

test('owner navigation keeps Team separate and nested deep links activate their parent', () => {
  const nav = load('lib/nav.ts');
  assert.ok(nav.primaryNavItems.some(item => item.href === '/team' && item.capability === 'team'));
  for (const [url, label] of [['/team/me', 'Me'], ['/team/activity', 'Team activity'], ['/team/board', 'Board']]) {
    assert.equal(nav.locate(url).item.label, label);
    assert.equal(nav.activeSection(url), 'team');
    assert.equal(nav.adminOnly(url), false);
  }
  assert.equal(nav.activeSection('/indexing'), 'projects');
  assert.equal(nav.activeSection('/packs/1504'), 'memory');
  assert.equal(nav.activeSection('/review'), 'memory');
  // Reranker is a configuration panel, so it now lives under Settings; the
  // saved reports it used to sit beside stayed with Quality as a tab.
  assert.equal(nav.activeSection('/reranker'), 'settings');
  assert.equal(nav.activeSection('/reports/42'), 'quality');
  assert.equal(nav.activeSection('/admin'), 'settings');
  assert.equal(nav.locate('/memory-other'), null);
  assert.ok(nav.allNavItems.some(item => item.href === '/setup'));
  // Help sits in the rail footer and the palette, outside the seven daily destinations.
  assert.ok(!nav.primaryNavItems.some(item => item.href === '/help'));
  assert.equal(nav.activeSection('/help'), 'help');
  assert.equal(nav.withProject('/quality', '3'), '/quality?repo=3');
  assert.equal(nav.withProject('/packs?view=fresh', '3'), '/packs?view=fresh&repo=3');
  assert.equal(nav.withProject('/setup#agent', '3'), '/setup?repo=3#agent');
  assert.equal(nav.withProject('/?repo=3&period=30d', ''), '/?period=30d');
});

test('dashboard request scope fails closed and distinguishes unavailable measurements from zero', async () => {
  const calls = [];
  let unavailable = false;
  const dashboard = load('lib/dashboard.ts', {
    'next/server': { connection: async () => {} },
    './api': { apiConfigured: true, brainFetch: async path => {
      calls.push(path);
      return unavailable ? { unavailable: true, total: null } : { total: 0 };
    } },
  });
  assert.equal(await dashboard.getRequestTelemetry('7d', 0), null);
  assert.equal(calls.length, 0);
  assert.equal((await dashboard.getRequestTelemetry('24h', 3)).total, 0);
  assert.equal(calls[0], '/api/web/dashboard?period=24h&repository_id=3');
  await dashboard.getRequestTelemetry('30d');
  assert.equal(calls[1], '/api/web/dashboard?period=30d');
  unavailable = true;
  assert.equal(await dashboard.getRequestTelemetry('7d'), null);
  assert.equal(dashboard.measuredNumber(0), '0');
  assert.equal(dashboard.measuredNumber(null), '—');
  assert.equal(dashboard.dashboardPeriod('unsupported'), '7d');
});

test('demo dashboard intervals are valid, cover the selected period, and reconcile to totals', async () => {
  const dashboard = load('lib/dashboard.ts', {
    'next/server': { connection: async () => {} },
    './api': { apiConfigured: false, brainFetch: async () => { throw new Error('demo must not call the API'); } },
  });
  const expectedLengths = { '24h': 8, '7d': 7, '30d': 30 };
  for (const period of Object.keys(expectedLengths)) {
    const telemetry = await dashboard.getRequestTelemetry(period);
    assert.equal(telemetry.buckets.length, expectedLengths[period]);
    const times = telemetry.buckets.map(bucket => {
      assert.match(bucket.at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
      const time = Date.parse(bucket.at);
      assert.ok(Number.isFinite(time));
      assert.ok(time <= Date.parse(telemetry.observed_at));
      return time;
    });
    const expectedStep = period === '24h' ? 3 * 60 * 60 * 1000 : 24 * 60 * 60 * 1000;
    assert.ok(times.every((time, i) => i === 0 || time - times[i - 1] === expectedStep));
    assert.equal(telemetry.buckets.reduce((sum, bucket) => sum + bucket.total, 0), telemetry.total);
    assert.equal(telemetry.buckets.reduce((sum, bucket) => sum + bucket.failed, 0), telemetry.failed);
  }
});

test('demo repository telemetry and agents stay scoped, and unknown repositories fail closed', async () => {
  const dashboard = load('lib/dashboard.ts', {
    'next/server': { connection: async () => {} },
    './api': { apiConfigured: false, brainFetch: async () => { throw new Error('demo must not call the API'); } },
  });
  const globalTelemetry = await dashboard.getRequestTelemetry('7d');
  const scopedTelemetry = await dashboard.getRequestTelemetry('7d', 3);
  assert.equal(scopedTelemetry.total, 3300);
  assert.notEqual(scopedTelemetry.total, globalTelemetry.total);
  assert.ok(scopedTelemetry.recent_requests.length > 0);
  assert.ok(scopedTelemetry.recent_requests.every(request => request.repository_id === 3));

  const scopedAgents = await dashboard.getAgentValue('7d', 3);
  assert.equal(scopedAgents.packs, null);
  assert.equal(scopedAgents.rules, null);
  assert.equal(scopedAgents.agents.map(agent => agent.agent).join(','), 'codex');
  assert.equal(scopedAgents.agents[0].requests, scopedTelemetry.total);
  assert.equal(scopedAgents.agents[0].failures, scopedTelemetry.failed);
  assert.notEqual(scopedAgents.agents[0].requests, (await dashboard.getAgentValue('7d')).agents[0].requests);

  for (const id of [0, -1, 5, 1.5, NaN]) {
    assert.equal(await dashboard.getRequestTelemetry('7d', id), null);
    assert.equal(await dashboard.getAgentValue('7d', id), null);
  }
});

test('agent value reports absence as absence rather than as a zero score', async () => {
  const calls = [];
  let answer;
  const dashboard = load('lib/dashboard.ts', {
    'next/server': { connection: async () => {} },
    './api': { apiConfigured: true, brainFetch: async path => { calls.push(path); return answer; } },
  });
  // An unresolved repository never widens into every project's traffic.
  assert.equal(await dashboard.getAgentValue('7d', 0), null);
  assert.equal(calls.length, 0);

  answer = { unavailable: true, packs: null, agents: [], rules: null };
  assert.equal(await dashboard.getAgentValue('30d', 3), null);
  assert.equal(calls[0], '/api/web/agent-value?period=30d&repository_id=3');

  answer = { unavailable: false, packs: { measured: 4, memory_reach: 0 }, agents: [], rules: null };
  const measured = await dashboard.getAgentValue('7d');
  assert.equal(calls[1], '/api/web/agent-value?period=7d');
  // A measured zero survives: no pack carried memory, which is a real answer.
  assert.equal(measured.packs.memory_reach, 0);
  assert.equal(dashboard.measuredShare(0), '0%');
  assert.equal(dashboard.measuredShare(0.6129), '61.3%');
  // Nothing recorded is a dash, so it can never be read as "nothing reached".
  assert.equal(dashboard.measuredShare(null), '—');
  assert.equal(dashboard.measuredShare(undefined), '—');
});

test('checked but unmerged decisions are not shown as agreed', async () => {
  const data = load('lib/data.ts', {
    'next/server': { connection: async () => {} },
    react: { cache: fn => fn },
    './api': {
      apiConfigured: true,
      brainFetch: async request => request.startsWith('/api/web/decisions') ? {
        decisions: [
          { id: 1, title: 'Pending merge', status: 'validated_unmerged' },
          { id: 2, title: 'Approved', status: 'accepted' },
        ],
        total: 2, page: 1, page_size: 20,
        facets: { status: { validated_unmerged: 1, accepted: 1 } },
      } : null,
    },
  });
  const result = await data.getPagedDecisions(undefined);
  assert.equal(result.items[0].status, 'validated_unmerged');
  assert.equal(result.items[1].status, 'accepted');
  assert.equal(result.paging.facets.validated_unmerged, 1);
});

test('reindex refuses an explicitly empty repository path but permits a scoped path', async () => {
  const calls = [];
  const jobs = load('lib/actions/jobs.ts', {
    '../api': {},
    './gate': {
      guard: () => null,
      mutate: async (path, options) => { calls.push({ path, options }); return { ok: true, message: 'queued' }; },
    },
  });
  for (const repoPath of ['', '   ']) {
    const result = await jobs.startReindex({ repoPath });
    assert.equal(result.ok, false);
    assert.equal(result.message, 'Choose a repository before starting a re-index.');
  }
  assert.equal(calls.length, 0);
  assert.deepEqual(await jobs.startReindex({ repoPath: '/repos/example' }), { ok: true, message: 'queued' });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, '/jobs/reindex');
  assert.equal(calls[0].options.body.repo_path, '/repos/example');
  assert.equal((await jobs.startReindex()).ok, true);
  assert.equal(calls[1].options.body.repo_path, undefined);
});

test('scoped corpus timeout is unavailable and never falls back to global indexing counts', async () => {
  const calls = [];
  const data = load('lib/data.ts', {
    'next/server': { connection: async () => {} },
    react: { cache: fn => fn },
    './api': {
      apiConfigured: true,
      brainFetch: async path => {
        calls.push(path);
        if (path === '/repositories') return { repositories: [{ id: 3, name: 'repo', path: '/repo' }] };
        if (path.includes('/api/web/overview')) return null;
        if (path === '/api/status/indexing') return { counts: { chunks: 54104 } };
        return null;
      },
    },
  });
  const corpus = await data.getCorpus('3');
  assert.equal(corpus.available, false);
  assert.equal(calls.includes('/api/status/indexing'), false);
});

test('index history distinguishes API timeout from a valid empty result', async () => {
  const fixture = (runs, calls) => load('lib/data.ts', {
    'next/server': { connection: async () => {} },
    react: { cache: fn => fn },
    './api': {
      apiConfigured: true,
      brainFetch: async path => {
        calls.push(path);
        if (path === '/repositories') return { repositories: [{ id: 3, name: 'repo', path: '/repo' }] };
        if (path.includes('/api/web/index-runs')) return runs;
        return null;
      },
    },
  });
  const timeoutCalls = [];
  const unavailable = await fixture(null, timeoutCalls).getPagedIndexRuns('3');
  assert.equal(unavailable.available, false);
  assert.equal(unavailable.items.length, 0);

  const emptyCalls = [];
  const available = await fixture({ runs: [], total: 0, page: 1, page_size: 20, facets: {} }, emptyCalls).getPagedIndexRuns('3');
  assert.equal(available.available, true);
  assert.deepEqual(available.items, []);
});

test('explicit and default repository corpus with missing counts never expands to global totals', async () => {
  const calls = [];
  const data = load('lib/data.ts', {
    'next/server': { connection: async () => {} },
    react: { cache: fn => fn },
    './api': {
      apiConfigured: true,
      brainFetch: async path => {
        calls.push(path);
        if (path === '/repositories') return { repositories: [{ id: 3, name: 'repo', path: '/repo' }] };
        if (path.includes('/api/web/overview')) return { counts: {} };
        if (path === '/api/status/indexing') return { counts: { chunks: 54104 } };
        return null;
      },
    },
  });
  for (const slug of ['3', undefined]) {
    const corpus = await data.getCorpus(slug);
    assert.equal(corpus.available, false);
    assert.equal(corpus.chunks, 0);
  }
  assert.equal(calls.includes('/api/status/indexing'), false);
});

test('live credentials require a valid configured gate; demo can remain public', async () => {
  const oldFetch = global.fetch;
  global.fetch = async () => { throw new Error('Brain is down'); };
  try {
    const { authorizeWebRequest: authorize } = load('lib/web-auth.ts');
    await withEnv({}, async () => assert.equal(await authorize(null), 200));
    // Malformed WEB_BASIC_AUTH, or no gate and a Brain that can't be asked: fail closed.
    for (const gate of [undefined, '', 'user', ':password', 'user:']) {
      await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture', WEB_BASIC_AUTH: gate }, async () => {
        assert.equal(await authorize(null), 503);
        assert.equal(await authorize(basic('user:password')), 503);
      });
    }
    await withEnv({ WEB_BASIC_AUTH: 'user:password' }, async () => {
      for (const header of [null, 'Bearer anything', 'Basic !!!', basic('user:wrong'), basic('other:password')]) {
        assert.equal(await authorize(header), 401);
      }
      assert.equal(await authorize(basic('user:password')), 200);
    });
  } finally { global.fetch = oldFetch; }
});

test('sign-in form issues a session cookie that is tied to the configured credentials', async () => {
  const { authorizeWebRequest: authorize, signIn, sessionFrom } = load('lib/web-auth.ts');
  const plain = result => ({ ...result });
  await withEnv({}, async () => assert.deepEqual(plain(await signIn('', '')), { status: 200, session: null }));
  await withEnv({ WEB_BASIC_AUTH: 'user:password' }, async () => {
    assert.deepEqual(plain(await signIn('user', 'wrong')), { status: 401 });
    assert.deepEqual(plain(await signIn('other', 'password')), { status: 401 });
    const ok = await signIn('user', 'password');
    assert.equal(ok.status, 200);
    assert.equal(await authorize(null, ok.session), 200);
    assert.equal(await authorize(null, sessionFrom(`theme=dark; pb_session=${ok.session}`)), 200);
    for (const forged of ['', 'x', ok.session + 'x']) assert.equal(await authorize(null, forged || null), 401);
    await withEnv({ WEB_BASIC_AUTH: 'user:rotated' }, async () => assert.equal(await authorize(null, ok.session), 401));
  });
});

// A stand-in for /api/web-login on the Brain API.
function fakeBrainLogin(initial = { mode: 'open', user: null, version: '0' }, password = null) {
  const brain = { state: { ...initial }, password, down: false, throttled: false, calls: [] };
  brain.fetch = async (url, options = {}) => {
    const u = new URL(String(url));
    brain.calls.push(`${options.method ?? 'GET'} ${u.pathname}`);
    if (brain.down) throw new Error('down');
    assert.equal(options.headers['X-API-Key'], 'fixture');
    if (u.pathname.endsWith('/api/web-login')) return Response.json(brain.state);
    if (u.pathname.endsWith('/api/web-login/verify')) {
      if (brain.throttled) return Response.json({ detail: 'Too many wrong passwords.' }, { status: 429 });
      const body = JSON.parse(options.body);
      if (brain.state.mode === 'open') return Response.json({ ok: false, mode: 'open' });
      return Response.json({ ok: body.user === brain.state.user && body.password === brain.password, mode: 'password', version: brain.state.version });
    }
    return brain.other ? brain.other(u, options) : new Response('not found', { status: 404 });
  };
  return brain;
}

test('Brain-kept login: open only in WEB_OPEN_MODE until set, then a password whose sessions die when it changes', async () => {
  const brain = fakeBrainLogin();
  const oldFetch = global.fetch;
  global.fetch = brain.fetch;
  try {
    const auth = load('lib/web-auth.ts');
    const plain = result => ({ ...result });
    await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture' }, async () => {
      // Fresh install, no opt-in: locked for everyone, nothing to sign in with.
      assert.equal(await auth.authorizeWebRequest(null), 503);
      assert.equal(await auth.authorizeWebRequest(basic('anyone:anything')), 503);
      assert.deepEqual(plain(await auth.signIn('', '')), { status: 503 });
      assert.equal((await auth.lockReason()).reason, 'unconfigured');
      // Explicit local-dev opt-in: in without a password.
      await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture', WEB_OPEN_MODE: 'true' }, async () => {
        assert.equal(await auth.authorizeWebRequest(null), 200);
        assert.deepEqual(plain(await auth.signIn('', '')), { status: 200, session: null });
        assert.equal(await auth.lockReason(), null);
      });

      // The owner sets a password (on Access, which also clears this process's memory).
      Object.assign(brain.state, { mode: 'password', user: 'owner', version: 'v1' });
      brain.password = 'correct horse';
      auth.forgetLoginState();
      assert.equal(await auth.authorizeWebRequest(null), 401);
      assert.equal(await auth.authorizeWebRequest(basic('owner:wrong one')), 401);
      assert.equal(await auth.authorizeWebRequest(basic('owner:correct horse')), 200);
      assert.deepEqual(plain(await auth.signIn('owner', 'nope')), { status: 401 });
      const ok = await auth.signIn('owner', 'correct horse');
      assert.equal(ok.status, 200);
      assert.equal(await auth.authorizeWebRequest(null, ok.session), 200);
      assert.equal(ok.session, auth.sessionForLogin('owner', 'v1'));
      for (const forged of ['x', ok.session + 'x', auth.sessionForLogin('owner', 'v0')]) assert.equal(await auth.authorizeWebRequest(null, forged), 401);

      // Changed from another browser or instance: this process still has v1 cached,
      // but a session it doesn't recognise makes it look again, and the old one fails.
      brain.state.version = 'v2';
      const fresh = auth.sessionForLogin('owner', 'v2');
      assert.equal(await auth.authorizeWebRequest(null, fresh), 200);
      assert.equal(await auth.authorizeWebRequest(null, ok.session), 401);

      // Brute force is refused by Brain and shown as such.
      brain.throttled = true;
      assert.deepEqual(plain(await auth.signIn('owner', 'guess')), { status: 429 });
      brain.throttled = false;

      // Brain goes away: the last known state keeps working, a cold process fails closed.
      brain.down = true;
      assert.equal(await auth.authorizeWebRequest(null, fresh), 200);
      auth.forgetLoginState();
      assert.equal(await auth.authorizeWebRequest(null, fresh), 503);
      assert.deepEqual(plain(await auth.signIn('owner', 'correct horse')), { status: 503 });
    });
  } finally { global.fetch = oldFetch; }
});

test('password removed in another instance: open only in WEB_OPEN_MODE, locked otherwise', async () => {
  const brain = fakeBrainLogin({ mode: 'password', user: 'owner', version: 'v1' }, 'correct horse');
  const oldFetch = global.fetch;
  global.fetch = brain.fetch;
  try {
    const auth = load('lib/web-auth.ts');
    await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture' }, async () => {
      const old = auth.sessionForLogin('owner', 'v1');
      assert.equal(await auth.authorizeWebRequest(null), 401);
      Object.assign(brain.state, { mode: 'open', user: null, version: 'v2' });
      // Without the opt-in, no login means locked, even for the old cookie once
      // this process looks again (cached state lives at most STATE_TTL_MS).
      auth.forgetLoginState();
      assert.equal(await auth.authorizeWebRequest(null, old), 503);
      assert.deepEqual({ ...(await auth.signIn('anyone', 'anything')) }, { status: 503 });
      assert.equal(await auth.authorizeWebRequest(null), 503);
    });
    Object.assign(brain.state, { mode: 'password', user: 'owner', version: 'v1' });
    auth.forgetLoginState();
    await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture', WEB_OPEN_MODE: 'true' }, async () => {
      const old = auth.sessionForLogin('owner', 'v1');
      assert.equal(await auth.authorizeWebRequest(null), 401);
      Object.assign(brain.state, { mode: 'open', user: null, version: 'v2' });
      // The browser that removed it keeps its old cookie and stays in.
      assert.equal(await auth.authorizeWebRequest(null, old), 200);
      // Anyone else who lands on the sign-in page gets in, and the cache is dropped.
      assert.deepEqual({ ...(await auth.signIn('anyone', 'anything')) }, { status: 200, session: null });
      assert.equal(await auth.authorizeWebRequest(null), 200);
    });
  } finally { global.fetch = oldFetch; }
});

test('WEB_BASIC_AUTH wins over the Brain-kept login and never sends the password to Brain', async () => {
  const brain = fakeBrainLogin({ mode: 'open', user: null, version: '0' });
  const oldFetch = global.fetch;
  global.fetch = brain.fetch;
  try {
    const auth = load('lib/web-auth.ts');
    await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture', WEB_BASIC_AUTH: 'user:password' }, async () => {
      assert.equal(await auth.authorizeWebRequest(null), 401);
      assert.equal(await auth.authorizeWebRequest(basic('user:password')), 200);
    });
    // Only the cached team-state probe (a team Brain refuses the server key); the password stays here.
    assert.deepEqual(brain.calls, ['GET /api/web-login']);
    // Brain away: WEB_BASIC_AUTH still decides, exactly as before team mode.
    brain.down = true;
    auth.forgetLoginState();
    await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture', WEB_BASIC_AUTH: 'user:password' }, async () => {
      assert.equal(await auth.authorizeWebRequest(null), 401);
      assert.equal(await auth.authorizeWebRequest(basic('user:password')), 200);
    });
  } finally { global.fetch = oldFetch; }
});

test('route handler enforces auth even without the proxy and never forwards an anonymous request', async () => {
  const brain = fakeBrainLogin({ mode: 'password', user: 'owner', version: 'v1' }, 'correct horse');
  let calls = 0;
  brain.other = async (url, options) => {
    calls++;
    assert.equal(String(url), 'https://api.example/brain/api/web/reports?limit=2');
    assert.equal(options.headers['X-API-Key'], 'fixture');
    return Response.json({ reports: [] });
  };
  const oldFetch = global.fetch;
  global.fetch = brain.fetch;
  try {
    const auth = load('lib/web-auth.ts');
    const { GET } = load('app/api/brain/[...path]/route.ts', { '@/lib/web-auth': auth });
    const request = header => ({ headers: new Headers(header ? { authorization: header } : {}), nextUrl: new URL('https://web.example/api/brain/web/reports?limit=2') });
    const context = { params: Promise.resolve({ path: ['web', 'reports'] }) };
    await withEnv({ BRAIN_API_URL: 'https://api.example/brain', BRAIN_API_KEY: 'fixture' }, async () => {
      assert.equal((await GET(request(null), context)).status, 401);
      assert.equal((await GET(request(basic('owner:wrong')), context)).status, 401);
      assert.equal(calls, 0);
      assert.equal((await GET(request(basic('owner:correct horse')), context)).status, 200);
      assert.equal(calls, 1);
    });
    await withEnv({ BRAIN_API_URL: 'https://api.example/brain', BRAIN_API_KEY: 'fixture', WEB_BASIC_AUTH: 'user:password' }, async () => {
      const denied = await GET(request(null), context);
      assert.equal(denied.status, 401);
      assert.equal(denied.headers.get('cache-control'), 'no-store');
      assert.equal(calls, 1);
      assert.equal((await GET(request(basic('user:password')), context)).status, 200);
      assert.equal(calls, 2);
    });
  } finally { global.fetch = oldFetch; }
});

test('half-configured deployments fail closed with a reason instead of falling back to public data', async () => {
  const oldFetch = global.fetch;
  const oldError = console.error;
  const logged = [];
  console.error = (...args) => logged.push(args.join(' '));
  try {
    global.fetch = async () => { throw new Error('must not be called'); };
    const auth = load('lib/web-auth.ts');
    for (const env of [{ BRAIN_API_URL: 'https://api.example' }, { BRAIN_API_KEY: 'fixture' }]) {
      for (const open of [undefined, 'true']) {
        await withEnv({ ...env, WEB_OPEN_MODE: open }, async () => {
          assert.equal(await auth.authorizeWebRequest(null), 503);
          assert.deepEqual({ ...(await auth.signIn('a', 'b')) }, { status: 503 });
          const lock = await auth.lockReason();
          assert.equal(lock.reason, 'misconfigured');
          assert.match(lock.detail, env.BRAIN_API_URL ? /BRAIN_API_KEY is missing/ : /BRAIN_API_URL is missing/);
        });
      }
    }
    assert.ok(logged.some(line => line.includes('[web-auth]') && line.includes('BRAIN_API_KEY is missing')));

    // Brain reports "open" but only one of the WEB_LOGIN_* pair is in its environment.
    const brain = fakeBrainLogin({ mode: 'open', user: null, version: '0', from_env: ['WEB_LOGIN_USER'] });
    global.fetch = brain.fetch;
    const fresh = load('lib/web-auth.ts');
    await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture', WEB_OPEN_MODE: 'true' }, async () => {
      assert.equal(await fresh.authorizeWebRequest(null), 503);
      const lock = await fresh.lockReason();
      assert.equal(lock.reason, 'misconfigured');
      assert.match(lock.detail, /WEB_LOGIN_USER but not WEB_LOGIN_PASSWORD_HASH/);
      const res = fresh.webAuthResponse(503, lock);
      assert.equal(res.status, 503);
      assert.deepEqual(await res.json(), { error: 'misconfigured', detail: lock.detail });
    });
  } finally { global.fetch = oldFetch; console.error = oldError; }
});

test('open mode: on by default only on a loopback-only dashboard; WEB_OPEN_MODE overrides either way', () => {
  const { openMode } = load('lib/web-auth.ts');
  for (const [bind, flag, expected] of [
    [undefined, undefined, false], ['0', undefined, false], ['1', undefined, true],
    ['1', 'false', false], ['1', '0', false], ['1', 'true', true], [undefined, 'true', true], ['1', 'junk', true],
  ]) {
    withEnv({ WEB_LOOPBACK_BIND: bind, WEB_OPEN_MODE: flag }, () => assert.equal(openMode('localhost:3100'), expected, `${bind}/${flag}`));
  }
  // A same-host reverse proxy forwards the public Host: the loopback default must not open it.
  withEnv({ WEB_LOOPBACK_BIND: '1', WEB_OPEN_MODE: undefined }, () => {
    for (const host of ['127.0.0.1:3100', 'localhost', '[::1]:3100', '127.1.2.3']) assert.equal(openMode(host), true, host);
    for (const host of [null, '', 'brain.example.com', 'brain.example.com:443', '10.0.0.5:3100', 'localhost.example.com']) assert.equal(openMode(host), false, String(host));
  });
});

test('fresh live install on loopback opens without a password; the same install on a public address stays locked', async () => {
  const oldFetch = global.fetch;
  try {
    global.fetch = fakeBrainLogin({ mode: 'open', user: null, version: '0', from_env: [] }).fetch;
    const auth = load('lib/web-auth.ts');
    await withEnv({ BRAIN_API_URL: 'http://127.0.0.1:8000', BRAIN_API_KEY: 'fixture', WEB_LOOPBACK_BIND: '1' }, async () => {
      assert.equal(await auth.authorizeWebRequest(null, null, '127.0.0.1:3100'), 200);
      assert.equal(await auth.lockReason('127.0.0.1:3100'), null);
      assert.equal(await auth.authorizeWebRequest(null, null, 'brain.example.com'), 503);
      assert.equal((await auth.lockReason('brain.example.com')).reason, 'unconfigured');
    });
    await withEnv({ BRAIN_API_URL: 'http://127.0.0.1:8000', BRAIN_API_KEY: 'fixture' }, async () => {
      assert.equal(await auth.authorizeWebRequest(null, null, '127.0.0.1:3100'), 503);
      assert.equal((await auth.lockReason('127.0.0.1:3100')).reason, 'unconfigured');
    });
  } finally { global.fetch = oldFetch; }
});

test('serve.mjs binds loopback by default and marks it for web-auth', () => {
  const src = fs.readFileSync(path.join(root, 'scripts/serve.mjs'), 'utf8');
  assert.match(src, /\|\| "127\.0\.0\.1"/);
  assert.match(src, /if \(loopback\) env\.WEB_LOOPBACK_BIND = "1";\s*else delete env\.WEB_LOOPBACK_BIND;/);
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  assert.equal(pkg.scripts.dev, 'node scripts/serve.mjs dev');
  assert.equal(pkg.scripts.start, 'node scripts/serve.mjs start');
});

test('WEB_OPEN_MODE accepts only explicit truthy values', () => {
  const { openMode } = load('lib/web-auth.ts');
  for (const [value, expected] of [[undefined, false], ['', false], ['0', false], ['false', false], ['no', false], ['true', true], ['TRUE', true], ['1', true], [' yes ', true]]) {
    withEnv({ WEB_OPEN_MODE: value }, () => assert.equal(openMode(), expected, String(value)));
  }
});

test('first password can be set only in open mode (loopback from this machine or WEB_OPEN_MODE), and removing it is refused otherwise', async () => {
  const brain = fakeBrainLogin();
  const sent = [];
  const oldFetch = global.fetch;
  global.fetch = brain.fetch;
  try {
    const auth = load('lib/web-auth.ts');
    const reply = { mode: 'password', user: 'owner', version: 'v1' };
    let host = 'brain.example.com';
    const login = load('lib/actions/login.ts', {
      'next/headers': { cookies: async () => ({ set() {} }), headers: async () => new Headers({ host }) },
      'next/cache': { revalidatePath() {} },
      '../api': { apiConfigured: true, brainSend: async (method, path) => { sent.push(`${method} ${path}`); return { ok: true, status: 200, data: reply }; } },
      '../web-auth': auth,
    });
    await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture' }, async () => {
      const refused = await login.setWebLogin({ user: 'mallory', password: 'hijacked!!' });
      assert.equal(refused.ok, false);
      assert.match(refused.message, /reachable beyond this machine/);
      assert.deepEqual(sent, []);
    });
    // Loopback bind, but reached through a public Host (same-host reverse proxy): still refused.
    await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture', WEB_LOOPBACK_BIND: '1' }, async () => {
      assert.equal((await login.setWebLogin({ user: 'mallory', password: 'hijacked!!' })).ok, false);
      assert.deepEqual(sent, []);
    });
    await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture', WEB_OPEN_MODE: 'true' }, async () => {
      assert.equal((await login.setWebLogin({ user: 'owner', password: 'correct horse' })).ok, true);
      assert.deepEqual(sent, ['PUT /api/web-login']);
    });
    // Loopback bind opened from this machine: the first password may be set.
    host = '127.0.0.1:3100';
    await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture', WEB_LOOPBACK_BIND: '1' }, async () => {
      assert.equal((await login.setWebLogin({ user: 'owner', password: 'correct horse' })).ok, true);
      assert.deepEqual(sent, ['PUT /api/web-login', 'PUT /api/web-login']);
    });
    host = 'brain.example.com';
    Object.assign(brain.state, reply);
    await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture' }, async () => {
      const refused = await login.disableWebLogin({ currentPassword: 'correct horse' });
      assert.equal(refused.ok, false);
      assert.deepEqual(sent, ['PUT /api/web-login', 'PUT /api/web-login']);
    });
  } finally { global.fetch = oldFetch; }
});

test('proxy route: unconfigured live dashboard never forwards with the server key; unreachable Brain is JSON 503', async () => {
  const brain = fakeBrainLogin();
  let forwarded = 0;
  brain.other = async () => { forwarded++; throw new Error('connection refused'); };
  const oldFetch = global.fetch;
  global.fetch = brain.fetch;
  try {
    const auth = load('lib/web-auth.ts');
    const { GET } = load('app/api/brain/[...path]/route.ts', { '@/lib/web-auth': auth });
    const request = () => ({ headers: new Headers(), nextUrl: new URL('https://web.example/api/brain/web/reports') });
    const context = { params: Promise.resolve({ path: ['web', 'reports'] }) };
    await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture' }, async () => {
      const locked = await GET(request(), context);
      assert.equal(locked.status, 503);
      assert.equal(locked.headers.get('cache-control'), 'no-store');
      const body = await locked.json();
      assert.equal(body.error, 'unconfigured');
      assert.match(body.detail, /WEB_BASIC_AUTH/);
      assert.equal(forwarded, 0);
    });
    await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture', WEB_OPEN_MODE: 'true' }, async () => {
      const down = await GET(request(), context);
      assert.equal(forwarded, 1);
      assert.equal(down.status, 503);
      assert.match(down.headers.get('content-type'), /application\/json/);
      const body = await down.json();
      assert.equal(body.error, 'brain_unreachable');
      assert.equal(typeof body.detail, 'string');
    });
  } finally { global.fetch = oldFetch; }
});

test('robots and metadata: only pure demo may be indexed', () => {
  const robots = load('app/robots.ts');
  withEnv({}, () => assert.deepEqual({ ...robots.default().rules }, { userAgent: '*', allow: '/' }));
  for (const env of [{ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture' }, { BRAIN_API_URL: 'https://api.example' }, { BRAIN_API_KEY: 'fixture' }]) {
    withEnv(env, () => assert.deepEqual({ ...robots.default().rules }, { userAgent: '*', disallow: '/' }));
  }
  const layout = fs.readFileSync(path.join(root, 'app/layout.tsx'), 'utf8');
  assert.match(layout, /robots: demo \? \{ index: true, follow: true \} : \{ index: false, follow: false \}/);
  assert.match(layout, /openGraph: \{ title: "Project Brain", description: DESCRIPTION/);
});

test('setup shows the mock provider as a warning, never as a finished step', () => {
  const steps = fs.readFileSync(path.join(root, 'components/setup/SetupSteps.tsx'), 'utf8');
  assert.match(steps, /demo: "warn"/);
  assert.match(steps, /demo: "mock — smoke test only"/);
  const actions = fs.readFileSync(path.join(root, 'lib/actions/setup.ts'), 'utf8');
  assert.match(actions, /smoke test only, results not meaningful/);
});

test('selected repository scopes indexing and graph requests, and unknown freshness stays unknown', async () => {
  const calls = [];
  const data = load('lib/data.ts', {
    'next/server': { connection: async () => {} }, 'react': { cache: fn => fn },
    './mock': {}, './api': { apiConfigured: true, brainFetch: async endpoint => {
      calls.push(endpoint);
      if (endpoint === '/repositories') return { repositories: [
        { id: 5, name: 'default', freshness: { status: 'current', commits_behind: 0 } },
        { id: 7, name: 'selected', freshness: { status: 'unindexed', commits_behind: null } },
      ] };
      if (endpoint.startsWith('/api/web/index-runs')) return { runs: [] };
      if (endpoint.startsWith('/api/web/module-graph')) return { edges: [] };
      throw new Error(endpoint);
    } },
  });
  await data.getIndexRuns('7');
  await data.getModuleEdges('7');
  assert.ok(calls.includes('/api/web/index-runs?limit=200&repository_id=7'));
  assert.ok(calls.includes('/api/web/module-graph?repository_id=7'));
  const repo = await data.getRepository('7');
  assert.equal(repo.behind, null);
  const { repositoryStatus } = load('lib/repository-status.ts');
  assert.equal(repositoryStatus(repo).status, 'Not indexed yet');
  assert.equal(repositoryStatus(repo).tone, 'warn');
  for (const status of ['unknown', 'source_missing', 'stale', 'freshness_error']) {
    assert.notEqual(repositoryStatus({ ...repo, freshness: status }).tone, 'ok');
  }
});

test('uncommitted observations preserve known zero, unknown and invalid counts', () => {
  const { uncommittedCount, uncommittedMessage, repositoryStatus } = load('lib/repository-status.ts');
  assert.equal(uncommittedCount(0), 0);
  assert.equal(uncommittedCount(3), 3);
  for (const value of [null, undefined, -1, 0.5, Infinity, NaN, '3', {}, Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(uncommittedCount(value), null);
  }
  assert.equal(uncommittedMessage(1), '1 uncommitted change not indexed yet');
  assert.equal(uncommittedMessage(3), '3 uncommitted changes not indexed yet');
  const repo = { freshness: 'current', behind: 0 };
  assert.equal(repositoryStatus(repo, 3).tone, 'warn');
  assert.equal(repositoryStatus(repo, 3).status, uncommittedMessage(3));
  assert.equal(repositoryStatus(repo, 0).tone, 'ok');
  for (const status of ['source_missing', 'freshness_error', 'error']) {
    const state = repositoryStatus({ ...repo, freshness: status }, 3);
    assert.equal(state.tone, 'bad');
    assert.match(state.status, /uncommitted changes/);
    assert.match(state.status, /unavailable|failed/);
  }
  assert.equal(repositoryStatus({ ...repo, freshness: 'behind', behind: 30 }, 3).tone, 'bad');
});

test('dashboard and setup read the latest observation for the right repository, never an unknown zero', async () => {
  let run = { uncommitted_changes: 3 };
  const calls = [];
  const data = load('lib/data.ts', {
    'next/server': { connection: async () => {} }, react: { cache: fn => fn },
    './api': { apiConfigured: true, brainFetch: async endpoint => {
      calls.push(endpoint);
      if (endpoint === '/repositories') return { repositories: [
        { id: 5, name: 'other', path: '/Other' }, { id: 7, name: 'selected', path: '/Selected' },
      ] };
      if (endpoint.startsWith('/api/web/overview')) return run === null ? null : { counts: { files: 2 }, last_index_run: run };
      throw new Error(endpoint);
    } },
  });
  assert.equal((await data.getCorpus('7')).uncommitted, 3);
  assert.equal(await data.getLatestUncommittedChanges('/Selected'), 3);
  assert.ok(calls.includes('/api/web/overview?include_health=false&repository_id=7'));
  assert.equal(await data.getLatestUncommittedChanges('/selected'), null);
  assert.equal(await data.getLatestUncommittedChanges(''), null);
  assert.equal(calls.some(path => path.includes('/api/web/overview') && path.endsWith('repository_id=5')), false);
  run = { uncommitted_changes: 0 };
  assert.equal((await data.getCorpus('7')).uncommitted, 0);
  run = {};
  assert.equal((await data.getCorpus('7')).uncommitted, null);
  run = null;
  assert.equal((await data.getCorpus('7')).available, false);
  assert.equal(await data.getLatestUncommittedChanges('/Selected'), null);
});

test('sidebar pulse carries validated last-run uncommitted observations without another request', async () => {
  let count = 3;
  const calls = [];
  const { getPulse } = load('lib/actions/pulse.ts', {
    '../repository-status': load('lib/repository-status.ts'),
    '../data': { getRepository: async () => ({ id: 7 }) },
    '../api': { apiConfigured: true, brainFetch: async endpoint => {
      calls.push(endpoint);
      if (endpoint === '/health') return { status: 'ok', details: {} };
      if (endpoint.includes('/api/web/index-runs')) return { runs: [{ status: 'completed', uncommitted_changes: count }] };
      return {};
    } },
  });
  assert.equal((await getPulse('7')).index.uncommitted, 3);
  assert.equal(calls.filter(p => p.includes('index-runs')).length, 1);
  assert.ok(calls.includes('/api/web/index-runs?repository_id=7&page_size=1'));
  count = 0;
  assert.equal((await getPulse('7')).index.uncommitted, 0);
  count = undefined;
  assert.equal((await getPulse('7')).index.uncommitted, undefined);
  count = -1;
  assert.equal((await getPulse('7')).index.uncommitted, undefined);
});

test('Telegram test messaging requires both saved token and nonempty saved chat ID', () => {
  const { telegramConfigured } = load('lib/telegram.ts');
  const section = (saved, chat) => ({ fields: [
    { key: 'TELEGRAM_ALERT_BOT_TOKEN', kind: 'secret', set: saved, value: null },
    { key: 'TELEGRAM_ALERT_CHAT_ID', kind: 'text', value: chat },
  ] });
  assert.equal(telegramConfigured(section(false, '')), false);
  assert.equal(telegramConfigured(section(false, '-1001234567890')), false);
  assert.equal(telegramConfigured(section(true, '')), false);
  assert.equal(telegramConfigured(section(true, '   ')), false);
  assert.equal(telegramConfigured(section(true, '-1001234567890')), true);
  assert.equal(telegramConfigured(section(true, '  -1001234567890  ')), true);
  for (const invalid of [undefined, { fields: [] }, section(undefined, '-1001234567890'), section(true, null), section(true, false)]) {
    assert.equal(telegramConfigured(invalid), false);
  }
  const draft = section(false, '-1001234567890');
  draft.fields[0].value = 'unsaved-fixture-token';
  assert.equal(telegramConfigured(draft), false, 'a typed unsaved token is not configured on the server');
});

test('live setup exposes copyable agent configuration and connection command', async () => {
  const data = load('lib/data.ts', {
    'next/server': { connection: async () => {} }, 'react': { cache: fn => fn }, './mock': {},
    './api': { apiConfigured: true, brainFetch: async () => ({
      steps: [{ id: 'agent', status: 'action', command: 'claude mcp add brain', detail: 'Connect your agent' }],
      client_configs: { Cursor: { config: '{"mcpServers":{}}', where: 'Settings', cli: 'python -m apps.mcp_server.server' } },
    }) },
  });
  assert.equal((await data.getSetupSteps())[0].command, 'claude mcp add brain');
  assert.equal((await data.getClientConfigs())[0].config, '{"mcpServers":{}}');
});

test('every advertised navigation destination has a page', () => {
  const { allNavItems } = load('lib/nav.ts');
  for (const item of allNavItems) {
    assert.ok(fs.existsSync(path.join(root, 'app', item.href, 'page.tsx')), `${item.label}: ${item.href}`);
  }
});

test('saved content downloads authenticate before reading and preserve complete Markdown', async () => {
  let reads = 0;
  const auth = load('lib/web-auth.ts');
  const { downloadArtifact } = load('lib/artifact-download.ts', {
    './web-auth': auth,
    './artifacts': { getArtifact: async () => { reads++; return { filename: 'evaluation.md', content: '# Result\n\nComplete evidence\n' }; } },
  });
  await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture', WEB_BASIC_AUTH: 'user:password' }, async () => {
    const denied = await downloadArtifact({ headers: new Headers() }, 'reports', 'evaluation.md');
    assert.equal(denied.status, 401);
    assert.equal(reads, 0);
    const response = await downloadArtifact({ headers: new Headers({ authorization: basic('user:password') }) }, 'reports', 'evaluation.md');
    assert.equal(await response.text(), '# Result\n\nComplete evidence\n');
    assert.match(response.headers.get('content-disposition'), /evaluation\.md/);
    assert.match(response.headers.get('cache-control'), /no-store/);
    assert.equal(reads, 1);
  });
});

test('saved content rejects path escapes and external return destinations', () => {
  const artifacts = load('lib/artifacts.ts', { './api': {} });
  for (const value of ['../secret.md', 'folder/secret.md', 'folder\\secret.md', 'report.md\r\nX-Secret: yes', 'report.txt']) {
    assert.equal(artifacts.validArtifactId('reports', value), false);
  }
  assert.equal(artifacts.validArtifactId('reports', 'evaluation.md'), true);
  assert.equal(artifacts.artifactBack('reports', '/reports?q=eval&page=2'), '/reports?q=eval&page=2');
  for (const value of ['https://other.example/reports', '//other.example/reports', 'javascript:alert(1)', '/settings']) {
    assert.equal(artifacts.artifactBack('reports', value), '/reports');
  }
});

test('unknown repository never inherits another repository corpus or background activity', async () => {
  const calls = [];
  const data = load('lib/data.ts', {
    'next/server': { connection: async () => {} }, 'react': { cache: fn => fn }, './mock': {},
    './api': { apiConfigured: true, brainFetch: async endpoint => {
      calls.push(endpoint);
      if (endpoint === '/repositories') return { repositories: [{ id: 3, path: '/app', name: 'Brain' }] };
      throw new Error(`Unexpected request for unknown repository: ${endpoint}`);
    } },
  });
  assert.equal((await data.getCorpus('999')).chunks, 0);
  assert.equal((await data.getJobs('999')).length, 0);
  assert.equal((await data.getAgentRuns('999')).length, 0);
  assert.equal((await data.getEvents(50, '999')).length, 0);
  assert.ok(calls.every(endpoint => endpoint === '/repositories'));
});

test('list URLs preserve punctuation and validate paging before reaching the API', () => {
  const lists = load('lib/list-query.ts');
  const parsed = lists.getListQuery(new URLSearchParams('q=alpha%2Cbeta&page=11&page_size=50&status=fresh'));
  assert.equal(parsed.q, 'alpha,beta');
  assert.equal(parsed.page, 11);
  assert.equal(parsed.size, 50);
  const roundTrip = lists.getListQuery(new URLSearchParams(lists.listQueryParams(parsed)));
  assert.equal(JSON.stringify(roundTrip), JSON.stringify(parsed));
  for (const page of ['2x', '-1', '0', '9007199254740992']) assert.equal(lists.getListQuery({ page }).page, 1);
  assert.equal(lists.getListQuery({ size: 50 }).size, 50);
  assert.equal(lists.getListQuery({ page_size: '200' }).size, 20);
});

test('missing worker heartbeat: the rail says the worker is down and a queued job says how to start it', async () => {
  const hint = 'Worker is not running. Start it with `./scripts/start.sh` (or `python -m brain.workers.worker`; `docker compose up -d worker` when Brain runs in Docker).';
  let worker = { running: false, pools: null, hint };
  const replies = {
    '/health': () => ({ status: 'ok', details: { postgres: { status: 'healthy' } }, scheduler: {}, worker }),
    '/api/version': () => ({ version: '1.0.0' }),
    '/api/setup/status': () => ({ steps: [] }),
    '/api/setup/providers': () => ({ current: { llm: { provider: 'openai', model: 'gpt-5' }, embedding: { provider: 'openai', model: 'text-embedding-3-small' } } }),
    '/jobs/job-1': () => ({ status: 'queued', progress: {} }),
  };
  const api = { apiConfigured: true, brainFetch: async (url) => (replies[url] ? replies[url]() : null), brainSend: async () => ({ ok: true, data: {} }) };
  const { getPulse } = load('lib/actions/pulse.ts', { '../api': api, '../data': { getRepository: async () => ({}) } });
  const down = await getPulse('');
  assert.equal(down.healthy, false);
  assert.deepEqual([...down.down], ['worker']);
  assert.equal(down.workerHint, hint);
  assert.equal(down.llm, 'openai / gpt-5');
  assert.equal(down.embedding, 'openai / text-embedding-3-small');

  const jobs = load('lib/actions/jobs.ts', { '../api': api, './gate': { guard: async () => null, mutate: async () => ({ ok: true }) } });
  const stalled = await jobs.getJob('job-1');
  assert.equal(stalled.stalled, true);
  assert.equal(stalled.detail, hint);

  worker = { running: true, pools: null, hint: '' };
  const up = await getPulse('');
  assert.equal(up.healthy, true);
  assert.deepEqual([...up.down], []);
  assert.equal(up.workerHint, undefined);
  const waiting = await jobs.getJob('job-1');
  assert.equal(waiting.stalled, false);
  assert.equal(waiting.status, 'queued');
});

test('setup repository step offers the repositories Brain has actually indexed', () => {
  const { indexedRepositories } = load('lib/setup-copy.ts');
  const step = { extra: { indexed_repositories: [{ name: 'app', path: '/home/u/app' }, { path: '/home/u/lib' }, { name: 'bad' }, null] } };
  assert.deepEqual(JSON.parse(JSON.stringify(indexedRepositories(step))), [
    { name: 'app', path: '/home/u/app' },
    { name: '/home/u/lib', path: '/home/u/lib' },
  ]);
  assert.deepEqual([...indexedRepositories({ extra: {} })], []);
});

test('quality distinguishes missing reference evidence from scheduled benchmarks', () => {
  const dash = load('lib/dashboard.ts', { 'next/server': { connection: async () => {} }, './api': { apiConfigured: true, brainFetch: async () => null } });
  assert.equal(dash.qualityNotRun({ available: false, reason: 'not_run' }), true);
  assert.equal(dash.qualityNotRun(null), false);
  assert.equal(dash.qualityNotRun({ historical: true }), false);
  const view = fs.readFileSync(path.join(root, 'components/QualityEvidence.tsx'), 'utf8');
  assert.match(view, /title="No saved reference measurements"/);
  assert.match(view, /href="\/reports"/);
  assert.match(view, /Scheduled smoke benchmarks are separate/);
  assert.match(view, /Reference run by maintainers/);
  // The period selector lives on the overview only; /quality has none to mention.
  assert.match(view, /compact \? "; the period selector does not change it" : ""/);
});

// A stand-in for a Brain with team members: it refuses the dashboard's server key
// ('fixture') and answers /api/web-session for each member's own credential.
function fakeTeamBrain(members = { pbk_alice: { id: 7, name: 'alice', admin: true }, pbk_bob: { id: 8, name: 'bob', admin: false } }) {
  const brain = { team: true, members: { ...members }, calls: [], forwarded: [] };
  brain.fetch = async (url, options = {}) => {
    const u = new URL(String(url));
    const key = options.headers['X-API-Key'];
    brain.calls.push(`${options.method ?? 'GET'} ${u.pathname} ${key}`);
    if (u.pathname.endsWith('/api/web-login')) {
      assert.equal(key, 'fixture');
      return brain.team
        ? Response.json({ detail: 'Named credential required: this Brain has team members' }, { status: 403 })
        : Response.json({ mode: 'open', user: null, version: '0' });
    }
    if (u.pathname.endsWith('/api/web-session')) {
      const member = brain.members[key];
      if (!brain.team) return Response.json({ mode: 'solo', principal: { id: 0, name: 'owner', kind: 'service' }, team_admin: true, member: null });
      if (!member) return Response.json({ detail: 'Dashboard sign-in denied: credential revoked' }, { status: 403 });
      return Response.json({ mode: 'team', principal: { id: member.id, name: member.name, kind: 'human' }, team_admin: member.admin, member: { is_admin: member.admin } });
    }
    brain.forwarded.push(key);
    return Response.json({ ok: true });
  };
  return brain;
}

const LIVE = { BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture' };

test('a saved member cookie rechecks a different process caching solo after bootstrap', async () => {
  await withEnv(LIVE, async () => {
    const previous = global.fetch;
    try {
      const brain = fakeTeamBrain();
      global.fetch = brain.fetch;
      brain.team = false;
      const auth = load('lib/web-auth.ts');
      const otherProcess = load('lib/web-auth.ts');
      assert.equal((await auth.loginState()).kind, 'brain');
      assert.equal((await otherProcess.loginState()).kind, 'brain');
      const sealed = auth.sealMember('pbk_alice');
      brain.team = true;
      assert.equal((await auth.memberFor(sealed)).name, 'alice');
      assert.equal(await otherProcess.authorizeWebRequest(null, null, 'dashboard.example', sealed), 200);
      assert.equal(await otherProcess.brainKeyFor(sealed), 'pbk_alice');
      assert.equal(await otherProcess.brainKeyFor(null), null, 'never fall back to the integration credential in team state');
      delete brain.members.pbk_alice;
      assert.equal(await otherProcess.authorizeWebRequest(null, null, 'dashboard.example', sealed), 401, 'the cookie never bypasses live revocation');
    } finally { global.fetch = previous; }
  });
});

test('member cookie: sealed by the server key, opaque, tamper-evident and short-lived', () => {
  const auth = load('lib/web-auth.ts');
  withEnv(LIVE, () => {
    const now = Date.UTC(2026, 9, 16);
    const sealed = auth.sealMember('pbk_alice', now);
    assert.equal(auth.openMember(sealed, now), 'pbk_alice');
    assert.doesNotMatch(Buffer.from(sealed, 'base64url').toString('latin1'), /pbk_alice/, 'the credential must not be readable from the cookie');
    assert.notEqual(auth.sealMember('pbk_alice', now), sealed, 'a fresh IV every time');
    const last = sealed.length - 2;
    const flipped = sealed.slice(0, last) + (sealed[last] === 'A' ? 'B' : 'A') + sealed.slice(last + 1);
    const unsealed = Buffer.from(JSON.stringify({ k: 'pbk_alice', iat: now / 1000 })).toString('base64url');
    for (const forged of [null, '', 'x', flipped, unsealed]) assert.equal(auth.openMember(forged, now), null);
    assert.equal(auth.openMember(sealed, now + (auth.MEMBER_MAX_AGE + 1) * 1000), null, 'expired');
    assert.equal(auth.openMember(auth.sealMember('pbk_alice', now + 3600_000), now), null, 'issued in the future');
    assert.equal(auth.memberFrom(`theme=dark; pb_member=${sealed}; pb_session=x`), sealed);
    withEnv({ ...LIVE, BRAIN_API_KEY: 'rotated' }, () => assert.equal(auth.openMember(sealed, now), null, 'rotating BRAIN_API_KEY signs members out'));
  });
});

test('team state: members sign in with their own key, every request is re-checked, revocation applies at once', async () => {
  const brain = fakeTeamBrain();
  const oldFetch = global.fetch;
  global.fetch = brain.fetch;
  try {
    const auth = load('lib/web-auth.ts');
    await withEnv(LIVE, async () => {
      assert.equal((await auth.loginState()).kind, 'team');
      // The owner login and an anonymous visitor no longer get in.
      assert.equal((await auth.signIn('owner', 'pw')).status, 401);
      assert.equal(await auth.authorizeWebRequest(null), 401);
      assert.equal((await auth.signInMember('pbk_mallory')).status, 401);
      assert.equal((await auth.signInMember('')).status, 401);
      const ok = await auth.signInMember('  pbk_bob ');
      assert.equal(ok.status, 200);
      assert.deepEqual({ ...ok.member }, { id: 8, name: 'bob', admin: false });
      assert.equal(await auth.authorizeWebRequest(null, null, null, ok.cookie), 200);
      assert.equal(await auth.brainKeyFor(ok.cookie), 'pbk_bob');
      assert.equal(await auth.brainKeyFor(null), null, 'no cookie: nothing to send, never the server key');
      assert.equal((await auth.memberFor(ok.cookie)).name, 'bob');
      // Revoked in Brain: the very next request is refused, nothing cached.
      delete brain.members.pbk_bob;
      assert.equal(await auth.authorizeWebRequest(null, null, null, ok.cookie), 401);
      assert.equal(await auth.memberFor(ok.cookie), null);
    });
    const checks = brain.calls.filter(c => c.includes('/api/web-session'));
    assert.ok(checks.length >= 5, 'every check asks Brain');
    assert.ok(checks.every(c => !c.endsWith(' fixture')), 'identity checks use the member key, never the server key');
  } finally { global.fetch = oldFetch; }
});

test('team state: the /api/brain proxy forwards only the signed-in member key', async () => {
  const brain = fakeTeamBrain();
  const oldFetch = global.fetch;
  global.fetch = brain.fetch;
  try {
    const auth = load('lib/web-auth.ts');
    const { GET } = load('app/api/brain/[...path]/route.ts', { '@/lib/web-auth': auth });
    const request = cookie => ({ headers: new Headers(cookie ? { cookie } : {}), nextUrl: new URL('https://web.example/api/brain/web/reports') });
    const context = { params: Promise.resolve({ path: ['web', 'reports'] }) };
    await withEnv(LIVE, async () => {
      assert.equal((await GET(request(null), context)).status, 401);
      assert.equal((await GET(request(`pb_session=${'x'.repeat(43)}`), context)).status, 401);
      const alice = await auth.signInMember('pbk_alice');
      assert.equal((await GET(request(`pb_member=${alice.cookie}`), context)).status, 200);
      assert.deepEqual(brain.forwarded, ['pbk_alice']);
      delete brain.members.pbk_alice;
      assert.equal((await GET(request(`pb_member=${alice.cookie}`), context)).status, 401);
      assert.deepEqual(brain.forwarded, ['pbk_alice']);
    });
  } finally { global.fetch = oldFetch; }
});

test('team state: server-rendered API calls carry the member key, never the server key, and skip the data cache', async () => {
  const brain = fakeTeamBrain();
  const oldFetch = global.fetch;
  const seen = [];
  global.fetch = async (url, options = {}) => {
    if (!/\/api\/web-(login|session)$/.test(new URL(String(url)).pathname)) seen.push({ key: options.headers['X-API-Key'], cache: options.cache, next: options.next });
    return brain.fetch(url, options);
  };
  try {
    await withEnv(LIVE, async () => {
      let jar = null;
      const headers = { cookies: async () => ({ get: name => (name === 'pb_member' && jar ? { value: jar } : undefined) }) };
      const api = load('lib/api.ts', { 'next/headers': headers });
      // Not signed in: nothing reaches Brain.
      assert.equal((await api.brainRead('/repositories')).status, 401);
      assert.equal((await api.brainSend('POST', '/team/projects/members', {})).status, 401);
      assert.equal(seen.length, 0);
      jar = load('lib/web-auth.ts').sealMember('pbk_alice');
      assert.equal((await api.brainRead('/repositories', { revalidate: 60 })).ok, true);
      assert.equal((await api.brainSend('DELETE', '/team/projects/members/8')).ok, true);
      assert.deepEqual(seen.map(s => s.key), ['pbk_alice', 'pbk_alice']);
      assert.equal(seen[0].cache, 'no-store');
      assert.equal(seen[0].next, undefined);
    });
  } finally { global.fetch = oldFetch; }
});

test('solo state is unchanged: the server key is used and the last member leaving restores the owner login', async () => {
  const brain = fakeTeamBrain();
  brain.team = false;
  const oldFetch = global.fetch;
  global.fetch = brain.fetch;
  try {
    const auth = load('lib/web-auth.ts');
    await withEnv({ ...LIVE, WEB_OPEN_MODE: 'true' }, async () => {
      assert.equal((await auth.loginState()).kind, 'brain');
      assert.equal(await auth.brainKeyFor(null), 'fixture');
      assert.equal(await auth.authorizeWebRequest(null), 200);
      assert.equal((await auth.signInMember('pbk_alice')).status, 401);
      assert.equal(await auth.memberFor(auth.sealMember('pbk_alice')), null);
      // Team, then the last member leaves while this process still remembers team state.
      brain.team = true;
      auth.forgetLoginState();
      const alice = await auth.signInMember('pbk_alice');
      assert.equal(alice.status, 200);
      brain.team = false;
      assert.equal(await auth.authorizeWebRequest(null, null, null, alice.cookie), 401);
      assert.equal((await auth.loginState()).kind, 'brain', 'the solo answer forgets the cached team state');
      assert.equal(await auth.brainKeyFor(alice.cookie), 'fixture');
    });
  } finally { global.fetch = oldFetch; }
});

test('team sign-in through the proxy: httpOnly sealed cookie, raw key never in a response, sign-out clears it', async () => {
  const brain = fakeTeamBrain();
  const oldFetch = global.fetch;
  global.fetch = brain.fetch;
  try {
    const { NextRequest } = require('next/server');
    const auth = load('lib/web-auth.ts');
    const { proxy } = load('proxy.ts', { './lib/web-auth': auth, '@/components/Icon': { MARK_TOKENS: [] } });
    const post = fields => {
      const body = new URLSearchParams(fields);
      return new NextRequest('https://web.example/__login', { method: 'POST', body, headers: { 'content-type': 'application/x-www-form-urlencoded', accept: 'text/html' } });
    };
    const get = (path, cookie) => new NextRequest(`https://web.example${path}`, { headers: { accept: 'text/html', ...(cookie ? { cookie } : {}) } });
    await withEnv(LIVE, async () => {
      // Anonymous visitors get the member-key form.
      const form = await proxy(get('/team'));
      assert.equal(form.status, 401);
      assert.match(await form.text(), /name="credential"/);
      // A wrong key: the form again, no cookie.
      const wrong = await proxy(post({ credential: 'pbk_mallory', next: '/team' }));
      assert.equal(wrong.status, 401);
      assert.equal(wrong.headers.get('set-cookie'), null);
      // The right key: a sealed, httpOnly cookie and a redirect to where they were going.
      const ok = await proxy(post({ credential: 'pbk_alice', next: '/team' }));
      assert.equal(ok.status, 303);
      assert.equal(new URL(ok.headers.get('location')).pathname, '/team');
      const setCookie = ok.headers.get('set-cookie');
      assert.match(setCookie, /pb_member=[^;]+/);
      assert.match(setCookie, /HttpOnly/i);
      assert.match(setCookie, /Secure/i);
      assert.match(setCookie, /SameSite=lax/i);
      assert.doesNotMatch(setCookie, /pbk_alice/);
      const sealed = /pb_member=([^;]+)/.exec(setCookie)[1];
      assert.equal(auth.openMember(decodeURIComponent(sealed)), 'pbk_alice');
      // Signed in: the request goes through.
      const through = await proxy(get('/team', `pb_member=${sealed}`));
      assert.equal(through.headers.get('x-middleware-next'), '1');
      // Revoked: the next page view shows the form and clears the stale cookie.
      delete brain.members.pbk_alice;
      const stale = await proxy(get('/team', `pb_member=${sealed}`));
      assert.equal(stale.status, 401);
      assert.match(stale.headers.get('set-cookie') ?? '', /pb_member=;/);
      // Sign out clears both cookies.
      const out = await proxy(get('/__logout', `pb_member=${sealed}`));
      assert.equal(out.status, 303);
      const cleared = out.headers.get('set-cookie');
      assert.match(cleared, /pb_member=;/);
      assert.match(cleared, /pb_session=;/);
    });
  } finally { global.fetch = oldFetch; }
});

// Team single sign-on (lib/oidc.ts): a team Brain that also speaks /team/oidc/*.
// The provider round trip itself is the API's job (tests/test_team_oidc.py); here
// the browser half: state + binding cookie, no server key, sealed member cookie.
function fakeSsoBrain() {
  const brain = fakeTeamBrain();
  const teamFetch = brain.fetch;
  Object.assign(brain, { sso: true, starts: [], callbacks: [], logouts: [], forwarded: [], refuse: null, credential: 'pbk_alice' });
  brain.fetch = async (url, options = {}) => {
    const u = new URL(String(url));
    if (!u.pathname.startsWith('/team/oidc/')) return teamFetch(url, options);
    if (u.pathname === '/team/oidc/logout') {
      // The one authenticated SSO route: the member's own key, never the dashboard's.
      brain.logouts.push({ method: options.method, key: options.headers['X-API-Key'] });
      return Response.json({ revoked: true });
    }
    assert.equal(options.headers['X-API-Key'], undefined, 'single sign-on never sends the dashboard key');
    if (u.pathname === '/team/oidc/config') return Response.json({ enabled: brain.sso, required_for_invitations: false });
    if (!brain.sso) return Response.json({ detail: 'Single sign-on is not enabled' }, { status: 404 });
    const body = JSON.parse(options.body);
    brain.forwarded.push(options.headers['X-Forwarded-For']);
    if (u.pathname === '/team/oidc/start') {
      brain.starts.push(body);
      return Response.json({ authorization_url: `https://idp.example/authorize?state=${'s'.repeat(32)}`, state: 's'.repeat(32), purpose: body.invitation ? 'invitation' : 'signin' });
    }
    brain.callbacks.push(body);
    if (brain.refuse) return Response.json({ detail: brain.refuse }, { status: 403 });
    assert.equal(body.binding, brain.starts.at(-1).binding, 'the callback carries the binding from the start');
    return Response.json({ mode: 'signin', credential: brain.credential, member: { id: 7, name: 'alice' } });
  };
  return brain;
}

async function withSso(run) {
  const brain = fakeSsoBrain();
  const oldFetch = global.fetch;
  global.fetch = brain.fetch;
  try {
    const { NextRequest } = require('next/server');
    const auth = load('lib/web-auth.ts');
    const oidc = load('lib/oidc.ts', { './web-auth': auth });
    const { proxy } = load('proxy.ts', { './lib/web-auth': auth, './lib/oidc': oidc, '@/components/Icon': { MARK_TOKENS: [] } });
    const get = (path, cookie, extra = {}) => new NextRequest(`https://web.example${path}`, { headers: { accept: 'text/html', ...(cookie ? { cookie } : {}), ...extra } });
    const cookieOf = (res, name) => res.headers.getSetCookie().find(c => c.startsWith(`${name}=`));
    await withEnv(LIVE, () => run({ brain, auth, oidc, proxy, get, cookieOf }));
  } finally { global.fetch = oldFetch; }
}

test('team SSO: the sign-in page offers "Sign in with SSO" only when the Brain has it on', () => withSso(async ({ brain, oidc, proxy, get }) => {
  const on = await (await proxy(get('/team?tab=members'))).text();
  assert.match(on, /Sign in with SSO/);
  assert.match(on, /href="\/auth\/oidc\/start\?next=%2Fteam%3Ftab%3Dmembers"/);
  assert.match(on, /name="credential"/, 'the member-key form stays');
  brain.sso = false;
  oidc.forgetSso();
  assert.doesNotMatch(await (await proxy(get('/team'))).text(), /Sign in with SSO/);
}));

test('team SSO: start sends the browser to the provider with a binding cookie, invitation in the body', () => withSso(async ({ brain, proxy, get, cookieOf }) => {
  const res = await proxy(get('/auth/oidc/start?invitation=pbi_example&next=/team'));
  assert.equal(res.status, 303);
  assert.equal(new URL(res.headers.get('location')).origin, 'https://idp.example');
  assert.equal(res.headers.get('referrer-policy'), 'no-referrer');
  assert.equal(res.headers.get('cache-control'), 'no-store');
  assert.equal(brain.starts.length, 1);
  assert.equal(brain.starts[0].invitation, 'pbi_example');
  assert.ok(brain.starts[0].binding.length >= 43, 'a 256-bit binding secret');
  const flow = cookieOf(res, 'pb_oidc');
  assert.match(flow, /HttpOnly/i);
  assert.match(flow, /Secure/i);
  assert.match(flow, /SameSite=lax/i);
  assert.match(flow, /Path=\/auth\/oidc/);
  assert.match(flow, /Max-Age=600/);
  const decode = cookie => JSON.parse(Buffer.from(decodeURIComponent(/pb_oidc=([^;]+)/.exec(cookie)[1]), 'base64url').toString('utf8'));
  assert.equal(decode(flow).n, '/team');
  // Never a sign-in loop: an /auth/oidc "next" falls back to "/".
  const loop = await proxy(get('/auth/oidc/start?next=/auth/oidc/callback'));
  assert.equal(decode(cookieOf(loop, 'pb_oidc')).n, '/');
  brain.sso = false;
  const off = await proxy(get('/auth/oidc/start'));
  assert.equal(off.status, 404);
  assert.match(await off.text(), /Single sign-on is not enabled on this Brain/);
}));

test('team SSO: start and callback pass the visitor address on, so Brain throttles each visitor separately', () => withSso(async ({ brain, proxy, get, cookieOf }) => {
  // The rightmost X-Forwarded-For hop is the one the dashboard's own front proxy wrote; a forged prefix is dropped.
  const start = await proxy(get('/auth/oidc/start?next=/team', null, { 'x-forwarded-for': '198.51.100.9, 203.0.113.5' }));
  const flow = /pb_oidc=([^;]+)/.exec(cookieOf(start, 'pb_oidc'))[1];
  await proxy(get(`/auth/oidc/callback?state=${'s'.repeat(32)}&code=c1`, `pb_oidc=${flow}`, { 'x-real-ip': '2001:db8::7' }));
  await proxy(get('/auth/oidc/start', null, { 'x-forwarded-for': 'not-an-address' }));
  assert.deepEqual(brain.forwarded, ['203.0.113.5', '2001:db8::7', undefined]);
}));

test('team SSO: the callback opens a member session only for the browser that started it', () => withSso(async ({ brain, auth, proxy, get, cookieOf }) => {
  const start = await proxy(get('/auth/oidc/start?next=/team'));
  const flow = /pb_oidc=([^;]+)/.exec(cookieOf(start, 'pb_oidc'))[1];
  const state = 's'.repeat(32);
  // No cookie (another browser) or a different state: refused before Brain is asked.
  for (const [path, cookie] of [[`/auth/oidc/callback?state=${state}&code=c1`, null], [`/auth/oidc/callback?state=${'t'.repeat(32)}&code=c1`, `pb_oidc=${flow}`]]) {
    const refused = await proxy(get(path, cookie));
    assert.equal(refused.status, 401);
    assert.match(await refused.text(), /started in another browser/);
    assert.match(cookieOf(refused, 'pb_oidc'), /pb_oidc=;/);
  }
  assert.equal(brain.callbacks.length, 0);
  // The provider said no.
  const denied = await proxy(get('/auth/oidc/callback?error=access_denied&state=x', `pb_oidc=${flow}`));
  assert.match(await denied.text(), /Sign-in was cancelled at the identity provider/);
  const odd = await proxy(get('/auth/oidc/callback?error=%3Cscript%3E', `pb_oidc=${flow}`));
  assert.doesNotMatch(await odd.text(), /<script>alert|\(%3Cscript|\(<script>\)/);
  // Brain refuses the identity: its reason is shown, escaped.
  brain.refuse = 'Your <b>email</b> does not match this invitation';
  const mismatch = await proxy(get(`/auth/oidc/callback?state=${state}&code=c1`, `pb_oidc=${flow}`));
  assert.equal(mismatch.status, 403);
  const html = await mismatch.text();
  assert.match(html, /Single sign-on didn’t work/);
  assert.match(html, /Your &lt;b&gt;email&lt;\/b&gt; does not match this invitation/);
  assert.equal(cookieOf(mismatch, 'pb_member'), undefined);
  // Accepted: a sealed member cookie, the flow cookie cleared, back to where they were going.
  brain.refuse = null;
  const ok = await proxy(get(`/auth/oidc/callback?state=${state}&code=c2`, `pb_oidc=${flow}`));
  assert.equal(ok.status, 303);
  assert.equal(new URL(ok.headers.get('location')).pathname, '/team');
  assert.equal(ok.headers.get('referrer-policy'), 'no-referrer');
  assert.deepEqual(brain.callbacks.at(-1), { state, code: 'c2', binding: brain.starts[0].binding });
  const member = cookieOf(ok, 'pb_member');
  assert.match(member, /HttpOnly/i);
  assert.doesNotMatch(member, /pbk_alice/);
  const sealed = /pb_member=([^;]+)/.exec(member)[1];
  assert.equal(auth.openMember(decodeURIComponent(sealed)), 'pbk_alice');
  assert.match(cookieOf(ok, 'pb_oidc'), /pb_oidc=;/);
  assert.match(cookieOf(ok, 'pb_session'), /pb_session=;/);
  assert.equal((await proxy(get('/team', `pb_member=${sealed}`))).headers.get('x-middleware-next'), '1');
  // A credential Brain then refuses (revoked in between) never becomes a cookie.
  brain.credential = 'pbk_mallory';
  const revoked = await proxy(get(`/auth/oidc/callback?state=${state}&code=c3`, `pb_oidc=${flow}`));
  assert.equal(revoked.status, 401);
  assert.equal(cookieOf(revoked, 'pb_member'), undefined);
}));

test('team SSO: logout ends the session in Brain with the member key, then clears the cookies', () => withSso(async ({ brain, auth, proxy, get }) => {
  const sealed = encodeURIComponent(auth.sealMember('pbk_alice'));
  const out = await proxy(get('/__logout', `pb_member=${sealed}`));
  assert.equal(out.status, 303);
  assert.deepEqual(brain.logouts, [{ method: 'POST', key: 'pbk_alice' }]);
  assert.match(out.headers.get('set-cookie'), /pb_member=;/);
  // Signed out already, or Brain down: the cookies are cleared all the same.
  assert.equal((await proxy(get('/__logout'))).status, 303);
  assert.equal(brain.logouts.length, 1);
  const down = brain.fetch;
  brain.fetch = global.fetch = async () => { throw new TypeError('fetch failed'); };
  const offline = await proxy(get('/__logout', `pb_member=${sealed}`));
  global.fetch = down;
  assert.equal(offline.status, 303);
  assert.match(offline.headers.get('set-cookie'), /pb_member=;/);
}));

test('team SSO: the invite page links to "Sign in with SSO", which POSTs the token, only when SSO is on', () => withSso(async ({ brain, oidc, proxy, get }) => {
  const html = await (await proxy(get('/invite'))).text();
  assert.match(html, /<a class="btn btn--ghost" href="\/auth\/oidc\/start" data-invitation>Sign in with SSO<\/a>/);
  assert.match(html, /input\.name = "invitation"/, 'the token goes in a POST body, not the URL');
  assert.match(html, /name="token"/, 'the token form stays');
  brain.sso = false;
  oidc.forgetSso();
  assert.doesNotMatch(await (await proxy(get('/invite'))).text(), /Sign in with SSO/);
}));

test('reranker page explains a refused project folder instead of reporting an outage', async () => {
  let status = 400;
  const data = load('lib/data.ts', {
    'next/server': { connection: async () => {} }, react: { cache: fn => fn },
    './api': {
      apiConfigured: true,
      brainFetch: async () => null,
      readFailureNote: () => '',
      brainRead: async path => {
        if (path.startsWith('/repositories')) return { ok: true, status: 200, data: { repositories: [{ id: 1, name: 'repo', path: '/outside/repo' }] } };
        if (path.startsWith('/late-interaction/status')) return { ok: false, status, reason: 'http_error' };
        return { ok: false, status: 404, reason: 'not_found' };
      },
    },
  });
  const refused = await data.getReranker('1');
  assert.equal(refused.available, false);
  assert.match(refused.unavailableReason, /outside the repository folders the server may read/);
  // Any other failure is still the generic outage, with no folder explanation.
  status = 500;
  const outage = await data.getReranker('1');
  assert.equal(outage.available, false);
  assert.equal(outage.unavailableReason, undefined);
});


test('repository actions distinguish source recovery from indexing, including encoded repository IDs', () => {
  const { repositoryAction } = load('lib/repository-status.ts');
  const missing = repositoryAction({ slug: 'repo / one', name: 'nostia', freshness: 'source_missing', behind: 8 });
  assert.equal(missing.href, '/projects?repo=repo%20%2F%20one#source');
  assert.match(missing.label, /Check source/);
  assert.doesNotMatch(missing.label, /index/i);
  for (const freshness of ['behind', 'unindexed', 'freshness_error']) {
    assert.equal(repositoryAction({ slug: '7', name: 'api', freshness }).href, '/indexing?repo=7');
  }
});

test('pulse attention destinations expose their cause and respect member restrictions', () => {
  const { pulseDestination, pulseHeadline } = load('lib/pulse-presentation.ts');
  const warning = { reachable: true, healthy: false, down: [], warnings: ['weekly benchmark'] };
  assert.equal(pulseHeadline(warning), 'Needs attention');
  assert.equal(pulseDestination(warning), '/settings#scheduler');
  assert.equal(pulseDestination(warning, true), '/help#server-status');
  assert.equal(pulseDestination({ ...warning, down: ['postgres'] }), '/help#server-status');
  assert.equal(pulseDestination({ ...warning, reachable: false }), '/help#server-status');
});

test('settings delivery and scheduled job observations share explicit UTC formatting', async () => {
  const stamp = '2026-10-22T06:30:00+03:00';
  const expected = load('lib/time.ts').utcTime(stamp);
  const scheduler = load('lib/scheduler-view.ts', { './api': { brainFetch: async () => null } });
  const job = scheduler.toSchedulerView({ enabled: true, jobs: [{ job_type: 'benchmark', next_run_at: stamp, last_success_at: stamp }] }).jobs[0];
  assert.equal(job.lastSuccess, expected);
  assert.equal(job.nextRun, expected);
  assert.equal(job.lastFailure, undefined);
  const settings = load('lib/settings-view.ts', {
    'next/server': { connection: async () => {} },
    './api': { apiConfigured: true, brainFetch: async path => path === '/api/web/health'
      ? { orchestration: { diagnostics: { last_delivery: { status: 'sent', evaluated_at: stamp } } } }
      : path === '/api/settings' ? { sections: [] } : null },
  });
  assert.equal((await settings.getSettingsView()).telegram.last.at, expected);
  assert.match(expected, /03:30 UTC$/);
});


test('a missed health probe retains independently reported build identity', async () => {
  const { getPulse } = load('lib/actions/pulse.ts', {
    '../api': { apiConfigured: true, brainFetch: async path => path === '/api/version' ? { version: '0.9.2', build_sha: '4935387c685f' } : null },
    '../data': { getRepository: async () => ({}) },
  });
  const pulse = await getPulse('');
  assert.equal(pulse.reachable, false);
  assert.equal(pulse.version, '0.9.2');
  assert.equal(pulse.build, '4935387');
});


test('repository summary never calls known omitted working-tree edits up to date', () => {
  const { repositorySummary } = load('lib/repository-status.ts');
  const repos = [{ id: 1, freshness: 'current', behind: 0 }];
  const summary = repositorySummary(repos, new Map([[1, 3]]));
  assert.match(summary, /1 with uncommitted changes omitted by the last index/);
  assert.doesNotMatch(summary, /up to date/);
  assert.match(repositorySummary(repos, new Map([[1, 0]])), /up to date/);
  assert.match(repositorySummary(repos, new Map([[1, null]])), /up to date/);
});

test('diagnostic UTC timestamps distinguish attempts within the same minute', () => {
  const { utcTime } = load('lib/time.ts');
  assert.match(utcTime('2026-10-22T06:30:01+03:00', true), /03:30:01 UTC$/);
  assert.notEqual(utcTime('2026-10-22T03:30:01Z', true), utcTime('2026-10-22T03:30:02Z', true));
});

test('capabilities require an explicit true contract and fail closed on old or unavailable APIs', async () => {
  let reply = { ok: true, data: { version: '0.9.2' } };
  const { getCapabilities } = load('lib/capabilities.ts', {
    react: { cache: fn => fn }, './api': { apiConfigured: true, brainRead: async () => reply },
  });
  assert.equal((await getCapabilities()).state, 'legacy');
  assert.equal((await getCapabilities()).supported.length, 0);
  reply = { ok: false, status: 503, reason: 'http_error' };
  assert.equal((await getCapabilities()).state, 'unavailable');
  reply = { ok: true, data: { capabilities: { team: true, team_board: 'true', repository_registration: false } } };
  assert.deepEqual([...(await getCapabilities()).supported], ['team']);
  reply = { ok: true, data: null };
  assert.equal((await getCapabilities()).state, 'unavailable');
});

test('an API without identity metadata leaves current credential unknown, never guessed from its name', async () => {
  const data = load('lib/data.ts', {
    'next/server': { connection: async () => {} }, react: { cache: fn => fn },
    './api': { apiConfigured: true, brainFetch: async () => ({ principals: [
      { id: 1, name: 'web-ui-vercel', credentials: [{ id: 11, scopes: ['principals:write'] }] },
    ] }) },
  });
  const identity = (await data.getAccessView()).identities[0];
  assert.equal(identity.current, undefined);
  assert.equal(identity.keys[0].current, undefined);
});

test('adding a repository validates paths, preserves defaults and reports index failure without retrying registration', async () => {
  const writes = [];
  const projects = load('lib/actions/projects.ts', {
    '../capabilities': { getCapabilities: async () => ({ supported: ['repository_registration'] }) },
    '../api': { brainRead: async () => ({ ok: true, data: { team_state: 'solo' } }) },
    './gate': { mutate: async (path, opts) => { writes.push({ path, ...opts }); return { ok: true, message: 'registered' }; } },
    './jobs': { startReindex: async () => ({ ok: false, message: 'Worker unavailable' }) },
  });
  for (const path of ['', 'relative/path', '/repo/../other', '/repo\nother']) {
    assert.equal((await projects.addProject({ name: 'example', path, indexNow: true })).ok, false);
  }
  assert.equal(writes.length, 0);
  const registered = await projects.addProject({ name: 'example', path: '/repos/example', indexNow: true });
  assert.equal(registered.ok, true);
  assert.equal(registered.data.registered, true);
  assert.equal(registered.data.indexed, false);
  assert.match(registered.message, /registered.*Worker unavailable/);
  assert.equal(writes.length, 1);
  assert.equal(writes[0].path, '/workspace/repositories');
  assert.equal(writes[0].body.path, '/repos/example');
  assert.ok(!('target_repo_path' in writes[0].body), 'the default repository must not change');
});
