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
    process, Buffer, Response, URL, AbortSignal, console, fetch: global.fetch }, { filename });
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
