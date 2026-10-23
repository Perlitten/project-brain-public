const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript5');

const filename = path.resolve(__dirname, '../lib/atlas.ts');
const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const moduleExports = { exports: {} };
vm.runInNewContext(output, { module: moduleExports, exports: moduleExports.exports }, { filename });
const atlas = moduleExports.exports;
// Sentences are arrays of segments; flatten them the way a reader sees them.
const read = (segments) => segments.map((s) => typeof s === 'string' ? s : s.num ?? s.label).join('');
const project = (slug, behind, extra = {}) => ({ slug, name: slug, behind, ok: !behind, status: behind ? `${behind} changes not read yet` : 'Up to date', ...extra });
const mod = (name, chunks, outdated = 0, missing = 0, excluded = 0) => ({ name, chunks, current: chunks - outdated - missing, outdated, missing, excluded });

test('dashboard verdict names the worst project first and accounts for the rest', () => {
  const projects = [project('project-brain', 3), project('docs-site', 0), project('mobile-app', 0), project('billing-service', 41)];
  assert.equal(read(atlas.todayVerdict(projects)), 'billing-service is 41 changes behind its code. project-brain, 3. The other two are current.');
  const first = atlas.todayVerdict(projects).find((s) => typeof s === 'object' && 'num' in s);
  assert.equal(first.tone, 'bad');
  assert.equal(read(atlas.todayVerdict([project('a', 0), project('b', 0)])), 'All two projects are current with their code.');
  assert.equal(read(atlas.todayVerdict([project('a', 0)])), 'a is current with its code.');
  assert.equal(read(atlas.todayVerdict([project('a', 1), project('b', 0)])), 'a is 1 change behind its code. The other one is current.');
  assert.equal(atlas.todayVerdict([]).length, 0);
});

test('dashboard verdict tells an unreadable source apart from a stale index', () => {
  const missing = project('missing-checkout', 8, { freshness: 'source_missing', ok: false, status: 'Source unavailable' });
  assert.equal(read(atlas.todayVerdict([missing])), "Brain can't read the source of missing-checkout.");
  const unverified = project('docs', null, { ok: false, status: 'Index freshness unverified' });
  assert.equal(read(atlas.todayVerdict([unverified, project('b', 0)])), 'docs: index freshness unverified. The other one is current.');
  const many = ['a', 'b', 'c', 'd'].map((s, i) => project(s, 10 - i));
  assert.equal(read(atlas.todayVerdict(many)), 'a is 10 changes behind its code. b, 9. Two more are behind.');
});

test('module tiles put code that needs reading first and fold the rest into one tile', () => {
  const modules = [mod('big', 900), mod('stale', 100, 60, 10), mod('unread', 80, 5, 40), mod('empty', 0), mod('skipped', 20, 0, 0, 20)];
  modules[4].current = 0;
  const tiles = atlas.moduleTiles(modules, 8);
  assert.deepEqual(Array.from(tiles, (t) => t.name), ['stale', 'unread', 'big']);
  assert.deepEqual(Array.from(tiles, (t) => t.state), ['chg', 'miss', 'ok']);
  assert.equal(tiles.find((t) => t.name === 'big').span, 3);
  const crowded = Array.from({ length: 12 }, (_, i) => mod(`m${i}`, 100 + i));
  const folded = atlas.moduleTiles(crowded, 8);
  assert.equal(folded.length, 8);
  assert.equal(folded[7].more, 5);
  assert.equal(folded[7].name, '5 more');
  assert.equal(atlas.moduleNote({ changed: 60, unread: 10, chunks: 100 }), '60 changed');
  assert.equal(atlas.moduleNote({ changed: 0, unread: 3, chunks: 100 }), '3 not read');
  assert.equal(atlas.moduleNote({ changed: 0, unread: 0, chunks: 1840 }), '1,840');
  assert.deepEqual({ ...atlas.moduleTotals(modules) }, { current: 965, outdated: 65, missing: 50, excluded: 20 });
});

test('stored memory lands in the project whose path it carries, global memory in the shared region', () => {
  const repos = [{ slug: 'billing-service', name: 'Billing Service', path: '/srv/repos/billing-service/' }, { slug: 'docs', name: 'docs', path: 'D:\\code\\docs' }];
  assert.equal(atlas.regionFor('/srv/repos/billing-service', undefined, repos), 'billing-service');
  assert.equal(atlas.regionFor('d:/code/docs/', undefined, repos), 'docs');
  assert.equal(atlas.regionFor('/elsewhere/docs', undefined, repos), 'docs');
  assert.equal(atlas.regionFor(null, undefined, repos), atlas.EVERY_PROJECT);
  assert.equal(atlas.regionFor('system_global', undefined, repos), atlas.EVERY_PROJECT);
  assert.equal(atlas.regionFor('/srv/unknown', undefined, repos), atlas.EVERY_PROJECT);
  assert.equal(atlas.regionFor(undefined, 'billing-service', repos), 'billing-service');
});

test('memory verdict counts what is in use and names a rule only when the evidence is enough', () => {
  const d = (status) => ({ id: 'D', title: '', status, scope: '', recordedAt: '', summary: '' });
  const r = (id, delivered, caught) => ({ id, rule: '', severity: 'warn', scope: '', delivered30d: delivered, caught30d: caught });
  const decisions = [d('accepted'), d('accepted'), d('proposed'), d('superseded')];
  assert.equal(read(atlas.memoryVerdict({ decisions, rules: [r('R-1', 18, 2), r('R-9', 9, 3)] })), 'Three decisions and two rules are in use. Agents got the rules 27 times in 30 days. R-9 was caught broken three times in the 9 tasks it reached.');
  assert.equal(read(atlas.memoryVerdict({ decisions, rules: [r('R-1', 18, 2), r('R-2', 0, 0), r('R-3', 3, 2)] })), 'Three decisions and three rules are in use. Agents got the rules 21 times in 30 days. One rule reached no agent.');
  assert.equal(read(atlas.memoryVerdict({ decisions, rules: [{ id: 'R', rule: '', severity: 'warn', scope: '' }] })), 'Three decisions and one rule are in use.');
  assert.match(read(atlas.memoryVerdict({ decisions: [], rules: [] })), /^Nothing is recorded yet/);
  assert.equal(atlas.decisionState('validated_unmerged'), 'chg');
  assert.equal(atlas.decisionState('superseded'), 'miss');
  assert.equal(atlas.ruleSpan(18, 24), 3);
  assert.equal(atlas.ruleSpan(9, 24), 2);
  assert.equal(atlas.ruleSpan(undefined, 24), 2);
});
