const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript5');
const root = path.resolve(__dirname, '..');

// Compile lib/team-activity-view.ts with the Brain API boundary replaced.
function loadView(brainRead) {
  const filename = path.join(root, 'lib', 'team-activity-view.ts');
  const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const module = { exports: {} };
  const requireModule = (name) => {
    if (name === 'server-only') return {};
    if (name === './api') return { brainRead };
    return require(name);
  };
  vm.runInNewContext(output, { module, exports: module.exports, require: requireModule, URLSearchParams, Date, Number }, { filename });
  return module.exports;
}

test('activity filters drop malformed values and build the Brain query', () => {
  const view = loadView(async () => ({ ok: false, status: 0, reason: 'network' }));
  const project = '0f8c3a52-6f5e-4d55-9b3e-1f7c8a9d0b12';
  const filters = view.activityFilters({ project, repository: '7', actor: 'x', kind: 'membership', since: '2026-10-01', offset: '50' });
  assert.equal(filters.project, project);
  assert.equal(filters.repository, 7);
  assert.equal(filters.actor, undefined, 'a non-numeric actor is dropped');
  assert.equal(filters.kind, 'membership');
  const query = new URL(`http://brain${view.activityPath(filters)}`).searchParams;
  assert.equal(query.get('project_id'), project);
  assert.equal(query.get('repository_id'), '7');
  assert.equal(query.get('kind'), 'membership');
  assert.equal(query.get('since'), '2026-10-01T00:00:00Z');
  assert.equal(query.get('offset'), '50');
  assert.equal(query.get('actor_id'), null);

  const bad = view.activityFilters({ project: "x' or 1=1", kind: 'secrets', since: 'yesterday', repository: '-1', offset: '999999' });
  assert.deepEqual({ ...bad }, { project: undefined, repository: undefined, actor: undefined, kind: undefined, since: undefined, offset: 5000 });
  assert.equal(view.activityHref(filters, 0), `/team/activity?project=${project}&repository=7&kind=membership&since=2026-10-01`);
});

test('activity page maps Brain refusals instead of showing an empty feed', async () => {
  let status = 403;
  const view = loadView(async () => ({ ok: false, status, reason: 'http_error' }));
  assert.equal((await view.getActivity({ offset: 0 })).kind, 'denied');
  const scoped = await view.getActivity({ offset: 0, project: '0f8c3a52-6f5e-4d55-9b3e-1f7c8a9d0b12' });
  assert.equal(scoped.kind, 'denied');
  assert.match(scoped.reason, /can't read that project/);
  status = 422;
  assert.equal((await view.getActivity({ offset: 0 })).kind, 'invalid');
  status = 404;
  assert.equal((await view.getActivity({ offset: 0 })).kind, 'unavailable', 'an old server without the feed');
});

test('activity rows name the actor and the next page', async () => {
  const calls = [];
  const view = loadView(async (p, opts) => {
    calls.push([p, opts]);
    return { ok: true, status: 200, data: { has_more: true, next_offset: 50, events: [
      { id: 'event:1', kind: 'job.queued', occurred_at: '2026-10-18T09:30:00+00:00', summary: 'Queued reindex job',
        actor: { principal_id: 4, name: 'alice' }, project_id: null, repository_id: 2 },
      { id: 'invitation:1:created', kind: 'invitation.created', occurred_at: null, summary: 'Invitation issued for bob',
        actor: null, actor_label: 'oauth-client' },
      { kind: 'broken' },
    ] } };
  });
  const page = await view.getActivity({ offset: 0 });
  assert.equal(calls[0][1].fresh, true, 'activity is never served from the data cache');
  assert.equal(page.kind, 'ok');
  assert.equal(page.rows.length, 2, 'a malformed event is skipped');
  assert.equal(page.rows[0].actor, 'alice');
  assert.equal(page.rows[0].repositoryId, 2);
  assert.equal(page.rows[1].actor, 'oauth-client');
  assert.equal(page.hasMore, true);
  assert.equal(page.nextOffset, 50);
});

test('the Me view reads solo and member answers from /team/me', async () => {
  let answer = { principal: { id: 0, name: 'owner', kind: 'service' }, team_state: 'solo', is_team_admin: true,
    projects: [], access: { repositories: 'all', reason: 'Solo state' }, scopes: ['*'], effective_scopes: ['*'], can_read_activity: true };
  const view = loadView(async (p) => (p === '/team/me' ? { ok: true, status: 200, data: answer } : { ok: false, status: 404, reason: 'not_found' }));
  const solo = await view.getMeView();
  assert.equal(solo.kind, 'ok');
  assert.equal(solo.teamState, 'solo');
  assert.equal(solo.allRepositories, true);
  assert.deepEqual([...solo.effectiveScopes], ['*']);

  answer = { principal: { id: 2, name: 'carol', kind: 'human' }, team_state: 'team', is_team_admin: false,
    projects: [{ id: 'p-a', name: 'Alpha', role: 'contributor', disabled: false, effective_permissions: ['read', 'task.create'],
      repositories: [{ id: 1, name: 'A', path: '/synthetic/A' }] }],
    access: { repositories: 'projects', reason: 'Team member' }, scopes: ['core:write'], effective_scopes: ['core:read', 'core:write'], can_read_activity: false };
  const member = await view.getMeView();
  assert.equal(member.teamAdmin, false);
  assert.equal(member.allRepositories, false);
  assert.equal(member.projects[0].role, 'contributor');
  assert.equal(member.projects[0].repositories[0].path, '/synthetic/A');
  assert.equal(member.canReadActivity, false);
  const options = await view.activityProjects(member);
  assert.deepEqual(options.map((o) => o.name), ['Alpha'], 'a member is offered only their own projects');
});

test('Me and Team activity are settings tabs members can open, and resolve to their own nav entries', () => {
  const nav = fs.readFileSync(path.join(root, 'lib', 'nav.ts'), 'utf8');
  const meAt = nav.indexOf('href: "/team/me"');
  const activityAt = nav.indexOf('href: "/team/activity"');
  const teamAt = nav.indexOf('href: "/team",');
  assert.ok(meAt > 0 && activityAt > 0 && teamAt > meAt && teamAt > activityAt, 'the specific entries come before /team');
  const adminOnly = nav.match(/ADMIN_ONLY_KEYS[^=]*= new Set\(\[([^\]]*)\]/)[1];
  assert.doesNotMatch(adminOnly, /team/);
  const tabs = fs.readFileSync(path.join(root, 'components', 'SectionNav.tsx'), 'utf8');
  assert.match(tabs, /\["\/team\/me", "Me"\]/);
  assert.match(tabs, /\["\/team\/activity", "Team activity"\]/);
  // The Team page itself is owned elsewhere; these pages live beside it.
  assert.ok(fs.existsSync(path.join(root, 'app', 'team', 'me', 'page.tsx')));
  assert.ok(fs.existsSync(path.join(root, 'app', 'team', 'activity', 'page.tsx')));
});
