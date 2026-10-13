const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript5');
const source = fs.readFileSync(require('node:path').join(__dirname, '../lib/public-demo.ts'), 'utf8');
const moduleObject = { exports: {} };
vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText,
  { module: moduleObject, exports: moduleObject.exports, process });
const { publicDemoProblem } = moduleObject.exports;

test('explicit public demo rejects operator bindings, including partial credentials', () => {
  for (const field of ['BRAIN_API_URL', 'BRAIN_API_KEY', 'WEB_BASIC_AUTH']) {
    assert.match(publicDemoProblem({ BRAIN_DEMO_MODE: 'true', [field]: 'fixture' }), /separate protected deployment/);
  }
  assert.equal(publicDemoProblem({ BRAIN_DEMO_MODE: '1' }), null);
  assert.equal(publicDemoProblem({ BRAIN_DEMO_MODE: ' true ', BRAIN_API_KEY: ' ' }), null);
});

test('operator configuration remains valid when isolated demo mode is absent', () => {
  assert.equal(publicDemoProblem({ BRAIN_API_URL: 'https://fixture.invalid', BRAIN_API_KEY: 'fixture' }), null);
  assert.equal(publicDemoProblem({ BRAIN_DEMO_MODE: 'false', WEB_BASIC_AUTH: 'fixture:fixture' }), null);
});
