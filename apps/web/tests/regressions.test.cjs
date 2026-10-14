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
    if (name.startsWith('./')) return load(path.relative(root, path.resolve(path.dirname(filename), `${name}.ts`)), overrides);
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
  assert.ok(calls.includes('/api/web/overview?repository_id=4'));
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
        urls.push(String(url));
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

test('owner navigation has seven destinations and nested deep links activate their parent', () => {
  const nav = load('lib/nav.ts');
  assert.equal(nav.primaryNavItems.length, 7);
  assert.equal(nav.activeSection('/indexing'), 'projects');
  assert.equal(nav.activeSection('/packs/1504'), 'memory');
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

test('WEB_BASIC_AUTH wins over the Brain-kept login and never asks Brain', async () => {
  const brain = fakeBrainLogin({ mode: 'open', user: null, version: '0' });
  const oldFetch = global.fetch;
  global.fetch = brain.fetch;
  try {
    const auth = load('lib/web-auth.ts');
    await withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture', WEB_BASIC_AUTH: 'user:password' }, async () => {
      assert.equal(await auth.authorizeWebRequest(null), 401);
      assert.equal(await auth.authorizeWebRequest(basic('user:password')), 200);
    });
    assert.deepEqual(brain.calls, []);
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

test('quality shows "Run the benchmark" on a fresh install and labels a maintainers\' reference run', () => {
  const dash = load('lib/dashboard.ts', { 'next/server': { connection: async () => {} }, './api': { apiConfigured: true, brainFetch: async () => null } });
  assert.equal(dash.qualityNotRun({ available: false, reason: 'not_run' }), true);
  assert.equal(dash.qualityNotRun(null), false);
  assert.equal(dash.qualityNotRun({ historical: true }), false);
  const view = fs.readFileSync(path.join(root, 'components/QualityEvidence.tsx'), 'utf8');
  assert.match(view, /title="No benchmark run yet"/);
  assert.match(view, /label="Run the benchmark"/);
  assert.match(view, /Reference run by maintainers/);
  // The period selector lives on the overview only; /quality has none to mention.
  assert.match(view, /compact \? "; the period selector does not change it" : ""/);
});
