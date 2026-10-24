const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript5');
const root = path.resolve(__dirname, '..');
// Values from the vm realm have their own Array/Object prototypes; compare them as plain data.
const eq = (actual, expected, message) => assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected, message);

function compile(file, requireModule) {
  const filename = path.join(root, 'lib', file);
  const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const module = { exports: {} };
  vm.runInNewContext(output, {
    module, exports: module.exports, require: requireModule, URLSearchParams, Date, Number, Promise, Array, Map, Set, Object,
  }, { filename });
  return module.exports;
}

const demo = compile('board-demo.ts', require).DEMO_BOARD;
const model = compile('board-model.ts', require);

// Compile lib/board-view.ts (it re-exports lib/board-model.ts) with the Brain API boundary and the member lookup replaced.
function loadView({ apiConfigured = true, brainRead = async () => ({ ok: false, status: 0, reason: 'network' }), getMeView = async () => ({ kind: 'unavailable' }) } = {}) {
  return compile('board-view.ts', (name) => {
    if (name === 'server-only') return {};
    if (name === './api') return { apiConfigured, brainRead };
    if (name === './board-demo') return { DEMO_BOARD: demo };
    if (name === './board-model') return model;
    if (name === './team-activity-view') return { getMeView };
    return require(name);
  });
}

const REVIEWER = demo.project.effectivePermissions;
const CONTRIBUTOR = ['read', 'task.create', 'run.request', 'knowledge.propose'];
const byTitle = (board) => Object.fromEntries(board.tasks.map((t) => [t.title, t]));

test('a task sits in the column of its latest queue entry, else its plan state, else its status', () => {
  const view = loadView();
  const entry = (status) => ({ status });
  assert.equal(view.columnFor('running', 'created', entry('queued')), 'awaiting');
  assert.equal(view.columnFor('running', 'created', entry('needs_rebase')), 'needs_rebase');
  assert.equal(view.columnFor('running', 'created', entry('stale_instructions')), 'stale_instructions');
  assert.equal(view.columnFor(null, 'running', entry('integrated')), 'integrated');
  assert.equal(view.columnFor('ready', 'created', entry('rejected')), 'ready', 'a rejected entry falls back to the plan');
  assert.equal(view.columnFor('waiting', 'created', null), 'blocked');
  assert.equal(view.columnFor('blocked', 'created', null), 'blocked', 'a cancelled dependency blocks too');
  assert.equal(view.columnFor('done', 'created', null), 'integrated');
  assert.equal(view.columnFor('cancelled', 'created', null), null, 'cancelled subtasks are hidden');
  assert.equal(view.columnFor(null, 'in_progress', null), 'running');
  assert.equal(view.columnFor(null, 'created', null), 'ready');
  assert.equal(view.columnFor(null, 'completed', null), 'integrated');
  assert.equal(view.columnFor(null, 'failed', null), null);
  eq(view.BOARD_COLUMNS.map((c) => c.id), ['blocked', 'ready', 'running', 'awaiting', 'needs_rebase', 'stale_instructions', 'integrated']);
});

test('the board puts every demo task in its column with dependencies, leases, overlaps and the queue in order', () => {
  const view = loadView();
  const board = view.assembleProject(demo.project, demo.raw);
  const t = byTitle(board);
  assert.equal(t['Payment retries'].column, 'running');
  assert.equal(t['Retry policy module'].column, 'integrated');
  assert.equal(t['Wire retries into the checkout API'].column, 'awaiting');
  assert.equal(t['Retry metrics on the dashboard'].column, 'blocked');
  assert.equal(t['Idempotency keys for refunds'].column, 'ready');
  assert.equal(t['Document the retry policy'].column, 'stale_instructions');
  assert.equal(t['Upgrade the HTTP client'].column, 'needs_rebase');

  const metrics = t['Retry metrics on the dashboard'];
  assert.equal(metrics.parentId, t['Payment retries'].id);
  assert.equal(metrics.parentTitle, 'Payment retries');
  eq([...metrics.waitingOn], [t['Wire retries into the checkout API'].id]);
  eq([...metrics.dependsOn], [t['Wire retries into the checkout API'].id]);
  eq([...t['Upgrade the HTTP client'].scopePaths], ['brain/http/**', 'apps/api/checkout/client.py'], 'a top-level task reads its own plan');
  assert.equal(t['Upgrade the HTTP client'].parentId, null);

  eq(board.queue.map((e) => e.id), [2, 3, 4], 'pending entries only, oldest first');
  eq(board.leases.map((l) => [l.resource, l.holderId, l.fencingToken]), [['apps/api/checkout/handlers.py', 4, 3], ['db/migrations', 7, 8]]);
  assert.equal(board.overlaps.length, 2);
  eq(board.overlaps[1].paths.map((p) => [...p]), [['db/migrations/**', 'db/migrations']]);
  const superseded = board.instructions[t['Document the retry policy'].entry.runId].superseded;
  eq(superseded.map((s) => [s.kind, s.id, s.version, s.currentVersion, s.reason]), [['decision', 42, 3, 4, 'edited'], ['rule', 7, 2, 2, 'retired']]);
});

test('malformed rows, released leases and duplicate entries are dropped or merged', () => {
  const view = loadView();
  const raw = {
    ...demo.raw,
    leases: [...demo.raw.leases, { ...demo.raw.leases[0], id: 13, state: 'released' }, { id: 'x' }],
    overlaps: [...demo.raw.overlaps, { kind: 'other', task_id: 'x' },
      { kind: 'dependency', task_id: 'dep-1', other_task_id: 'dep-2', other_title: 'lib', relation: 'imports', paths: [['a.py', 'b.py']] }],
    entries: [...demo.raw.entries, demo.raw.entries[3], { id: 9, status: 'weird', task_id: 'x', run_id: 'y', version: 1 }],
  };
  const board = view.assembleProject(demo.project, raw);
  assert.equal(board.leases.length, 2);
  assert.equal(board.overlaps.length, 3, 'a dependency overlap is kept, an unknown kind is dropped');
  eq(board.overlaps.map((o) => o.kind).slice(-1), ['dependency']);
  eq(board.overlaps[2].paths, [['a.py', 'b.py']]);
  eq(board.queue.map((e) => e.id), [2, 3, 4]);
  // A newer final entry replaces the earlier pending one for the same task.
  const later = { ...demo.raw.entries[1], id: 5, status: 'integrated', version: 3 };
  const moved = view.assembleProject(demo.project, { ...demo.raw, entries: [...demo.raw.entries, later] });
  assert.equal(byTitle(moved)['Upgrade the HTTP client'].column, 'integrated');
});

test('integrate needs an independent reviewer; withdraw is for the member who queued it', () => {
  const view = loadView();
  const board = view.assembleProject(demo.project, demo.raw);
  const [rebase, stale, queued] = board.queue; // queued by 9, by me (4), by 7
  eq({ ...view.entryActions(rebase, REVIEWER, 4) }, { integrate: true, reject: true, withdraw: false, rerun: true });
  eq({ ...view.entryActions(stale, REVIEWER, 4) }, { integrate: false, reject: true, withdraw: false, rerun: true }, 'never your own entry');
  eq({ ...view.entryActions(stale, CONTRIBUTOR, 4) }, { integrate: false, reject: false, withdraw: true, rerun: true });
  eq({ ...view.entryActions(queued, CONTRIBUTOR, 4) }, { integrate: false, reject: false, withdraw: false, rerun: true });
  eq({ ...view.entryActions(queued, ['read'], 4) }, { integrate: false, reject: false, withdraw: false, rerun: false }, 'a viewer only reads');
  const done = { ...queued, status: 'integrated' };
  eq({ ...view.entryActions(done, REVIEWER, 4) }, { integrate: false, reject: false, withdraw: false, rerun: false });
  assert.equal(view.entryActions(rebase, REVIEWER, null).integrate, false);
  assert.equal(view.entryActions({ ...rebase, enqueuedBy: 4, initiatorId: 9, requestedBy: 9 }, REVIEWER, 4).integrate, true,
    'the member who queued another member\'s run may independently review it');
  assert.equal(view.entryActions({ ...rebase, enqueuedBy: 9, initiatorId: 4 }, REVIEWER, 4).integrate, false,
    'the run initiator may not integrate even when another member queued it');
  assert.equal(view.entryActions(stale, CONTRIBUTOR, null).withdraw, false, 'unknown member: nothing is theirs');
});

test('queue positions count only earlier queued entries in the same repository', () => {
  const view = loadView();
  const board = view.assembleProject(demo.project, demo.raw);
  const [rebase, stale, queued] = board.queue;
  assert.equal(view.queuePosition(board.queue, rebase), null);
  assert.equal(view.queuePosition(board.queue, stale), null);
  assert.equal(view.queuePosition(board.queue, queued), 1);
  const later = { ...queued, id: queued.id + 10 };
  const otherRepo = { ...queued, id: queued.id + 20, repositoryId: 99 };
  assert.equal(view.queuePosition([...board.queue, later, otherRepo], later), 2);
  assert.equal(view.queuePosition([...board.queue, later, otherRepo], otherRepo), 1);
});

test('the integrate dialog offers the newest integrated head in the repository, else the last checked base', () => {
  const view = loadView();
  const board = view.assembleProject(demo.project, demo.raw);
  const [rebase] = board.queue;
  const head = (id, sha, decidedAt, repositoryId = rebase.repositoryId) => ({ ...rebase, id, status: 'integrated', headSha: sha, decidedAt, repositoryId });
  const tasks = [
    { entry: head(7, 'a'.repeat(40), '2026-10-23T12:00:00Z') },
    { entry: head(1, 'b'.repeat(40), '2026-10-23T13:00:00Z') },
    { entry: head(9, 'c'.repeat(40), '2026-10-23T14:00:00Z', 99) },
    { entry: null },
  ];
  eq(view.knownBase({ tasks, queue: board.queue }, rebase), { sha: 'b'.repeat(40), source: 'integrated' }, 'latest decision wins even at an older ID; another repository never counts');
  eq(view.knownBase({ tasks: [], queue: [] }, { ...rebase, checkedBaseSha: 'd'.repeat(40) }), { sha: 'd'.repeat(40), source: 'checked' });
  assert.equal(view.knownBase({ tasks: [], queue: [] }, { ...rebase, checkedBaseSha: null }), null);
});

test('only the holder may release or renew a lease', () => {
  const view = loadView();
  const [mine, theirs] = view.assembleProject(demo.project, demo.raw).leases;
  eq({ ...view.leaseActions(mine, REVIEWER, 4) }, { release: true, renew: true });
  eq({ ...view.leaseActions(mine, ['read'], 4) }, { release: true, renew: false });
  eq({ ...view.leaseActions(theirs, REVIEWER, 4) }, { release: false, renew: false });
  eq({ ...view.leaseActions({ ...mine, state: 'expired' }, REVIEWER, 4) }, { release: false, renew: false });
});

test('superseded decisions and rules link to Memory', () => {
  const view = loadView();
  assert.equal(view.instructionHref({ kind: 'decision', id: 42 }), '/memory?view=decisions&q=42');
  assert.equal(view.instructionHref({ kind: 'rule', id: 7 }), '/memory?view=rules&q=7');
});

test('the board is demo data without an API, unavailable without a member, and absent in solo state', async () => {
  const offline = await loadView({ apiConfigured: false }).getBoardView();
  assert.equal(offline.kind, 'ok');
  assert.equal(offline.demo, true);
  assert.equal(offline.projects[0].name, 'Checkout');
  assert.equal((await loadView().getBoardView()).kind, 'unavailable');
  const solo = await loadView({ getMeView: async () => ({ kind: 'ok', teamState: 'solo', principal: { id: 1 }, projects: [] }) }).getBoardView();
  assert.equal(solo.kind, 'solo');
});

test('a team board reads every coordination route fresh with the member key and notes what Brain did not return', async () => {
  const calls = [];
  const base = `/team/projects/${demo.project.id}`;
  const brainRead = async (p, opts) => {
    calls.push([p, opts]);
    if (p === `${base}/tasks`) return { ok: true, status: 200, data: demo.raw.tasks };
    if (p === `${base}/leases`) return { ok: false, status: 503, reason: 'http_error' };
    if (p === `${base}/overlaps`) return { ok: true, status: 200, data: { overlaps: demo.raw.overlaps } };
    if (p === `${base}/integration`) return { ok: true, status: 200, data: { entries: demo.raw.entries.filter((e) => e.status !== 'integrated') } };
    if (p === `${base}/integration?include_done=true`) return { ok: false, status: 503, reason: 'http_error' };
    const plan = p.match(/\/tasks\/([^/]+)\/plan$/);
    if (plan) return demo.raw.plans[plan[1]] ? { ok: true, status: 200, data: demo.raw.plans[plan[1]] } : { ok: true, status: 200, data: { plan: { task_id: plan[1] }, subtasks: [] } };
    if (/\/runs\/[^/]+\/instructions$/.test(p)) return { ok: true, status: 200, data: demo.raw.instructions[0] };
    return { ok: false, status: 404, reason: 'http_error' };
  };
  const getMeView = async () => ({
    kind: 'ok', teamState: 'team', principal: { id: 4 },
    projects: [{ ...demo.project, disabled: false }, { ...demo.project, id: 'disabled', disabled: true }],
  });
  const view = await loadView({ brainRead, getMeView }).getBoardView();
  assert.equal(view.kind, 'ok');
  assert.equal(view.demo, false);
  assert.equal(view.selfId, 4);
  assert.equal(view.projects.length, 1, 'disabled projects are skipped');
  const board = view.projects[0];
  assert.ok(calls.every(([, opts]) => opts.fresh === true), 'never served from the data cache');
  assert.ok(calls.some(([p]) => p.endsWith('/instructions')), 'stale entries fetch their superseded instructions');
  eq(board.missing, ['leases', 'integration history']);
  assert.equal(board.leases.length, 0);
  assert.equal(byTitle(board)['Document the retry policy'].column, 'stale_instructions');
  eq(board.queue.map((e) => e.id), [2, 3, 4]);
});



test('the client Board imports only the pure model, never the server-only loader', () => {
  const board = fs.readFileSync(path.join(root, 'components', 'team', 'Board.tsx'), 'utf8');
  const model = fs.readFileSync(path.join(root, 'lib', 'board-model.ts'), 'utf8');
  assert.match(board, /^"use client";/);
  assert.match(board, /from "@\/lib\/board-model"/);
  assert.doesNotMatch(board, /lib\/board-view|server-only|team-activity-view"/);
  assert.doesNotMatch(model, /server-only|from "\.\/api"/);
  assert.match(model, /^import type \{ MeProject \}/m, 'a type-only import is erased from the client bundle');
});
