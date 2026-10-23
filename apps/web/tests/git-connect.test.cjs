// "Connect repository → Git URL": the form's URL guess mirrors the API's rules,
// and Settings shows where clones land.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript5');

const root = path.resolve(__dirname, '..');
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
  vm.runInNewContext(output, { module, exports: module.exports, require: requireModule, URL, console }, { filename });
  return module.exports;
}

const { parseGitRemote, probeSummary, PROVIDER_LABEL, TOKEN_HINT } = load('lib/git-remote.ts');

test('git URL guess recognises the hosting provider and repository name', () => {
  const gh = parseGitRemote(' https://github.com/Perlitten/project-brain-public.git ');
  assert.equal(gh.valid, true);
  assert.equal(gh.kind, 'https');
  assert.equal(gh.provider, 'github');
  assert.equal(gh.name, 'project-brain-public');
  assert.equal(gh.display, 'https://github.com/Perlitten/project-brain-public.git');
  assert.equal(gh.embeddedCredentials, false);

  const scp = parseGitRemote('git@gitlab.com:group/sub/repo.git');
  assert.equal(scp.valid, true);
  assert.equal(scp.kind, 'ssh');
  assert.equal(scp.provider, 'gitlab');
  assert.equal(scp.name, 'repo');

  const ssh = parseGitRemote('ssh://git@bitbucket.org/team/thing');
  assert.equal(ssh.kind, 'ssh');
  assert.equal(ssh.provider, 'bitbucket');
  assert.equal(ssh.name, 'thing');

  assert.equal(parseGitRemote('https://dev.azure.com/org/project/_git/repo').provider, 'azure');
  assert.equal(parseGitRemote('https://git.example.internal/x/y.git').provider, 'other');
  assert.equal(parseGitRemote('https://gitlab.example.com/x/y.git').provider, 'gitlab');
  for (const provider of Object.keys(PROVIDER_LABEL)) assert.ok(TOKEN_HINT[provider], provider);
});

test('git URL guess rejects what the API rejects, and flags embedded credentials', () => {
  const rejected = ['', '   ', '-oProxyCommand=evil', 'https://h/a b', 'file:///etc', 'ext::sh -c evil', 'git://host/repo', '/srv/repo', 'repo.git',
    'https://', 'https://github.com/', 'https://github.com/..', 'localhost-noslash:path', 'x'.repeat(1001)];
  for (const url of rejected) {
    const guess = parseGitRemote(url);
    assert.equal(guess.valid, false, url);
    assert.ok(guess.problem, url);
  }
  // Only a real hostname counts for scp-like addresses; localhost is allowed.
  assert.equal(parseGitRemote('git@localhost:repo.git').valid, true);
  assert.equal(parseGitRemote('C:\\repos\\thing').valid, false);

  const leaked = parseGitRemote('https://me:ghp_secret@github.com/me/private.git');
  assert.equal(leaked.valid, true);
  assert.equal(leaked.embeddedCredentials, true);
  assert.equal(leaked.display, 'https://***@github.com/me/private.git');
  assert.ok(!JSON.stringify(leaked).includes('ghp_secret'));
});

test('probe summary explains reachability, auth and branch count in one sentence', () => {
  assert.equal(probeSummary({ ok: true, reachable: true, branches: [{ name: 'main', head: 'a' }, { name: 'dev', head: 'b' }], default_branch: 'main' }), 'Reachable: 2 branches, default main.');
  assert.equal(probeSummary({ ok: true, reachable: true, branches: [{ name: 'main', head: 'a' }] }), 'Reachable: 1 branch.');
  assert.equal(probeSummary({ ok: true, reachable: true, branches: [] }), 'Reachable: no branches listed.');
  assert.match(probeSummary({ ok: false, reachable: false, needs_auth: true }), /token/);
  assert.equal(probeSummary({ ok: false, reachable: false, error: 'could not resolve host' }), 'could not resolve host');
  assert.match(probeSummary({ ok: false, reachable: false }), /not reachable/);
});

test('settings git integration carries the clones directory only when the server reports it', async () => {
  const build = (managed_repos) => load('lib/settings-view.ts', {
    'next/server': { connection: async () => {} },
    './scheduler-view': { getSchedulerJobs: async () => null, demoScheduler: () => null },
    './time': { utcTime: (s) => s },
    './api': { apiConfigured: true, brainFetch: async (p) => p === '/api/settings' ? { sections: [], git: {
      webhook_path: '/webhooks/git-merge', workflow_example: 'docs/examples/reindex-on-push.yml',
      origin_remotes: { remotes: [], skipped: 0 }, ...(managed_repos === undefined ? {} : { managed_repos }) } } : null },
  });
  assert.equal((await build(undefined).getSettingsView()).git.managedRepos, undefined);
  assert.deepEqual({ ...(await build({ dir: null, writable: null }).getSettingsView()).git.managedRepos }, { dir: undefined, writable: undefined });
  assert.deepEqual({ ...(await build({ dir: "/var/lib/brain/repos", writable: true }).getSettingsView()).git.managedRepos }, { dir: '/var/lib/brain/repos', writable: true });
  assert.deepEqual({ ...(await build({ dir: "/ro", writable: false }).getSettingsView()).git.managedRepos }, { dir: '/ro', writable: false });
});

test('the capability contract advertises git_connect to the dashboard', () => {
  const { CAPABILITY_NAMES } = load('lib/capability-contract.ts');
  assert.ok(CAPABILITY_NAMES.includes('git_connect'));
  assert.ok(CAPABILITY_NAMES.includes('repository_registration'));
});
