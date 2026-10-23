const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript5');

const filename = path.resolve(__dirname, '../lib/code-graph.ts');
const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const moduleExports = { exports: {} };
vm.runInNewContext(output, { module: moduleExports, exports: moduleExports.exports }, { filename });
const { graphView, neighbours, graphLayout } = moduleExports.exports;
const edge = (from, to, weight = 2, violation = false) => Object.freeze({ from, to, weight, violation });
const edges = Object.freeze([
  edge('apps/api', 'brain/search', 4),
  edge('apps/mcp', 'brain/search', 3, true),
  edge('brain/search', 'brain/database', 5),
  edge('brain/database', 'brain/storage', 2),
  edge('scripts/cli', 'apps/api', 1),
  edge('tests/api', 'apps/api', 6),
  edge('eval/catalog', 'scripts/docs', 2),
]);
const options = (overrides = {}) => ({ minWeight: 2, includeTests: false, query: '', focus: null, hops: 1, expanded: new Set(), ...overrides });

test('areas start collapsed, tests and weight-one edges excluded without input mutation', () => {
  const view = graphView(edges, options());
  assert.ok(view.nodes.every(node => !node.module));
  assert.ok(view.nodes.every(node => node.area !== 'tests'));
  assert.equal(view.modules, 7);
  assert.equal(view.connections, 5);
  const combined = view.edges.find(item => item.from === 'area:apps' && item.to === 'area:brain');
  assert.equal(combined.weight, 7);
  assert.equal(combined.violation, true);
  assert.equal(edges.length, 7);
  assert.equal(edges[0].weight, 4);
});

test('expanding an area preserves its modules and aggregates only collapsed endpoints', () => {
  const expanded = new Set(['brain']);
  const view = graphView(edges, options({ expanded }));
  assert.equal(view.nodes.filter(node => node.module && node.area === 'brain').length, 3);
  assert.ok(view.edges.some(item => item.from === 'brain/search' && item.to === 'brain/database'));
  assert.equal(expanded.size, 1);
});

test('neighbourhoods include both import directions, are bounded to one or two hops', () => {
  assert.deepEqual(Array.from(neighbours(edges, 'brain/search', 1)).sort(), ['apps/api', 'apps/mcp', 'brain/database', 'brain/search']);
  const two = neighbours(edges, 'brain/search', 2);
  assert.ok(two.has('brain/storage'));
  assert.ok(two.has('tests/api'));
  assert.ok(!two.has('eval/catalog'));
});

test('persistent focus shows only bounded neighbour modules and dims unrelated areas', () => {
  const one = graphView(edges, options({ focus: 'brain/search' }));
  assert.equal(one.focus, 'brain/search');
  assert.ok(!one.nodes.some(node => node.id === 'brain/storage'));
  assert.ok(one.nodes.some(node => node.area === 'eval' && node.dimmed));
  assert.ok(one.edges.every(item => item.from !== 'eval/catalog'));
  const two = graphView(edges, options({ focus: 'brain/search', hops: 2 }));
  assert.ok(two.nodes.some(node => node.id === 'brain/storage'));
});

test('search is case-insensitive and expands matching modules; missing search is honest', () => {
  const view = graphView(edges, options({ query: ' BRAIN/SEARCH ' }));
  assert.equal(view.matches, 1);
  assert.ok(view.nodes.some(node => node.id === 'brain/search' && node.matched));
  assert.equal(graphView(edges, options({ query: 'not-a-module' })).matches, 0);
});

test('threshold and tests controls operate on the original module edges', () => {
  assert.ok(graphView(edges, options({ includeTests: true })).nodes.some(node => node.area === 'tests'));
  assert.ok(graphView(edges, options({ minWeight: 1 })).modules > graphView(edges, options()).modules);
  assert.equal(graphView(edges, options({ minWeight: 100 })).modules, 0);
  assert.equal(graphView(edges, options({ focus: 'missing' })).focus, null);
  assert.equal(graphView([], options()).nodes.length, 0);
});

test('all collapsed and expanded node bounds fit desktop and mobile viewboxes', () => {
  for (const width of [309, 390, 1143, 1440]) {
    for (const expanded of [new Set(), new Set(['apps', 'brain', 'eval', 'scripts', 'tests'])]) {
      const view = graphView(edges, options({ expanded, includeTests: true }));
      const layout = graphLayout(view.nodes, width);
      for (const position of layout.positions.values()) {
        assert.ok(position.x >= 0 && position.x + layout.nodeWidth <= width);
        assert.ok(position.y >= 0 && position.y + layout.nodeHeight <= layout.height);
      }
    }
  }
});

test('finding modules and file paths map to the module the graph draws', () => {
  const { moduleOf } = moduleExports.exports;
  assert.equal(moduleOf('brain/search'), 'brain/search');
  assert.equal(moduleOf('brain/search/code_search.py'), 'brain/search');
  assert.equal(moduleOf('apps\\api\\routers\\web.py'), 'apps/api');
  assert.equal(moduleOf('billing-service/payments/retry.py'), 'billing-service/payments');
  assert.equal(moduleOf('README.md'), null);
  assert.equal(moduleOf('—'), null);
  assert.equal(moduleOf('  '), null);
});

test('a map opened on a module focuses it when drawn and searches for it otherwise', () => {
  const { startingView } = moduleExports.exports;
  assert.deepEqual({ ...startingView(edges, null) }, { minWeight: 2, includeTests: false, query: '', focus: null });
  const known = startingView(edges, 'scripts/cli');
  assert.deepEqual({ ...known }, { minWeight: 1, includeTests: false, query: '', focus: 'scripts/cli' });
  // The weight-one edge stays on the map, so the focused module is really shown.
  assert.equal(graphView(edges, options(known)).focus, 'scripts/cli');
  assert.equal(startingView(edges, 'tests/api').includeTests, true);
  const unknown = startingView(edges, 'brain');
  assert.deepEqual({ ...unknown }, { minWeight: 1, includeTests: false, query: 'brain', focus: null });
  assert.ok(graphView(edges, options(unknown)).matches > 0);
});
