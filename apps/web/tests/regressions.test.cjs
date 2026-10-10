const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const root = path.resolve(__dirname, '..');

// Compile the actual TypeScript modules; isolate only Next request and API boundaries.
function load(file, overrides = {}) {
  const filename = path.join(root, file);
  const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const module = { exports: {} };
  const requireModule = (name) => {
    if (Object.hasOwn(overrides, name)) return overrides[name];
    if (name === 'server-only') return {};
    if (name.startsWith('./')) return load(path.relative(root, path.resolve(path.dirname(filename), `${name}.ts`)), overrides);
    return require(name);
  };
  vm.runInNewContext(output, { module, exports: module.exports, require: requireModule,
    process, Buffer, Response, URL, URLSearchParams, AbortSignal, console, fetch: global.fetch }, { filename });
  return module.exports;
}

function withEnv(values, run) {
  const names = ['WEB_BASIC_AUTH', 'BRAIN_API_URL', 'BRAIN_API_KEY'];
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
  assert.equal(nav.activeSection('/reranker'), 'quality');
  assert.equal(nav.activeSection('/admin'), 'settings');
  assert.equal(nav.locate('/memory-other'), null);
  assert.ok(nav.allNavItems.some(item => item.href === '/setup'));
  assert.equal(nav.withProject('/quality', '3'), '/quality?repo=3');
  assert.equal(nav.withProject('/packs?view=fresh', '3'), '/packs?view=fresh&repo=3');
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

test('live credentials require a valid configured gate; demo can remain public', () => {
  const { authorizeWebRequest: authorize } = load('lib/web-auth.ts');
  withEnv({}, () => assert.equal(authorize(null), 200));
  for (const gate of [undefined, '', 'user', ':password', 'user:']) {
    withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture', WEB_BASIC_AUTH: gate }, () => {
      assert.equal(authorize(null), 503);
      assert.equal(authorize(basic('user:password')), 503);
    });
  }
  withEnv({ WEB_BASIC_AUTH: 'user:password' }, () => {
    for (const header of [null, 'Bearer anything', 'Basic !!!', basic('user:wrong'), basic('other:password')]) {
      assert.equal(authorize(header), 401);
    }
    assert.equal(authorize(basic('user:password')), 200);
  });
});

test('sign-in form issues a session cookie that is tied to the configured credentials', () => {
  const { authorizeWebRequest: authorize, signIn, sessionFrom } = load('lib/web-auth.ts');
  const plain = result => ({ ...result });
  withEnv({}, () => assert.deepEqual(plain(signIn('', '')), { status: 200, session: null }));
  withEnv({ BRAIN_API_URL: 'https://api.example', BRAIN_API_KEY: 'fixture' }, () => assert.deepEqual(plain(signIn('user', 'password')), { status: 503 }));
  withEnv({ WEB_BASIC_AUTH: 'user:password' }, () => {
    assert.deepEqual(plain(signIn('user', 'wrong')), { status: 401 });
    assert.deepEqual(plain(signIn('other', 'password')), { status: 401 });
    const ok = signIn('user', 'password');
    assert.equal(ok.status, 200);
    assert.equal(authorize(null, ok.session), 200);
    assert.equal(authorize(null, sessionFrom(`theme=dark; pb_session=${ok.session}`)), 200);
    for (const forged of ['', 'x', ok.session + 'x']) assert.equal(authorize(null, forged || null), 401);
    withEnv({ WEB_BASIC_AUTH: 'user:rotated' }, () => assert.equal(authorize(null, ok.session), 401));
  });
});

test('route handler enforces auth even without the proxy and never forwards an anonymous request', async () => {
  const auth = load('lib/web-auth.ts');
  const oldFetch = global.fetch;
  let calls = 0;
  global.fetch = async (url, options) => {
    calls++;
    assert.equal(String(url), 'https://api.example/brain/api/web/reports?limit=2');
    assert.equal(options.headers['X-API-Key'], 'fixture');
    return Response.json({ reports: [] });
  };
  try {
    const { GET } = load('app/api/brain/[...path]/route.ts', { '@/lib/web-auth': auth });
    const request = header => ({ headers: new Headers(header ? { authorization: header } : {}), nextUrl: new URL('https://web.example/api/brain/web/reports?limit=2') });
    const context = { params: Promise.resolve({ path: ['web', 'reports'] }) };
    await withEnv({ BRAIN_API_URL: 'https://api.example/brain', BRAIN_API_KEY: 'fixture' }, async () => {
      assert.equal((await GET(request(null), context)).status, 503);
      assert.equal(calls, 0);
    });
    await withEnv({ BRAIN_API_URL: 'https://api.example/brain', BRAIN_API_KEY: 'fixture', WEB_BASIC_AUTH: 'user:password' }, async () => {
      const denied = await GET(request(null), context);
      assert.equal(denied.status, 401);
      assert.equal(denied.headers.get('cache-control'), 'no-store');
      assert.equal(calls, 0);
      assert.equal((await GET(request(basic('user:password')), context)).status, 200);
      assert.equal(calls, 1);
    });
  } finally { global.fetch = oldFetch; }
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
