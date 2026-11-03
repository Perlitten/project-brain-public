const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript5');

const root = path.resolve(__dirname, '..');
const presets = [
  { name: 'local', label: 'Offline identifier features', requiresKey: false, embeddings: true, llm: false, embeddingDimension: 384 },
  { name: 'disabled', label: 'LLM intentionally disabled', requiresKey: false, embeddings: false, llm: true },
  { name: 'openai', label: 'OpenAI', requiresKey: true, embeddings: true, llm: true, baseUrl: 'https://api.openai.com/v1' },
];

function compile(file, requireModule, suffix = '') {
  const output = ts.transpileModule(fs.readFileSync(path.join(root, file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const module = { exports: {} };
  vm.runInNewContext(output + suffix, { module, exports: module.exports, require: requireModule });
  return module.exports;
}

// Controlled component contracts, not browser rendering or live-provider proof.
function form(slot, provider, dimension, success = false) {
  const state = [];
  let cursor = 0;
  let tree;
  const submitted = [];
  let current = { provider, dimension, native: ['local', 'disabled'].includes(provider) };
  const { ProviderForm } = compile('components/setup/SetupSteps.tsx', name => {
    if (name === 'react') return { useState: initial => {
      const i = cursor++;
      if (!(i in state)) state[i] = typeof initial === 'function' ? initial() : initial;
      return [state[i], value => { state[i] = typeof value === 'function' ? value(state[i]) : value; }];
    } };
    if (name === '@/components/act') return { useAction: () => ({ pending: false, mode: 'on', run: async input => {
      submitted.push(JSON.parse(JSON.stringify(input)));
      return { ok: success };
    } }) };
    if (name === 'react/jsx-runtime') return require(name);
    return {};
  }, '\nexports.ProviderForm = ProviderForm;');
  const render = () => {
    cursor = 0;
    tree = ProviderForm({ slot, presets, current });
  };
  const walk = node => !node || typeof node !== 'object' ? [] : Array.isArray(node)
    ? node.flatMap(walk) : [node, ...walk(node.props?.children)];
  render();
  return { nodes: () => walk(tree), submitted,
    submit: () => tree.props.onSubmit({ preventDefault() {} }),
    refresh: value => { current = value; render(); },
    pick: name => {
      walk(tree).find(n => n.type === 'button' && walk(n.props.children)
        .some(child => child.type === 'b' && child.props.children === name)).props.onClick();
      render();
    },
    change: value => {
      walk(tree).find(n => n.type === 'input').props.onChange({ target: { value } });
      render();
    },
  };
}

test('disabled language model saves without a URL, model or API key and excludes embeddings-only presets', () => {
  const h = form('llm', 'disabled');
  assert.equal(h.nodes().some(n => n.type === 'input'), false);
  assert.equal(h.nodes().some(n => n.props?.children === 'Offline identifier features'), false);
  assert.equal(h.nodes().find(n => n.props?.type === 'submit').props.disabled, false);
  h.submit();
  assert.deepEqual(h.submitted, [{ slot: 'llm', provider: 'disabled' }]);
});

test('local embeddings need no endpoint or key and preserve the saved dimension', () => {
  const h = form('embedding', 'local', 384);
  assert.equal(h.nodes().filter(n => n.type === 'input').length, 1);
  assert.equal(h.nodes().some(n => n.props?.type === 'password'), false);
  assert.equal(h.nodes().find(n => n.props?.type === 'submit').props.disabled, false);
  h.submit();
  assert.deepEqual(h.submitted, [{ slot: 'embedding', provider: 'local' }]);
});

test('local vector-size changes are explicit and do not silently reset an incompatible saved size', () => {
  const h = form('embedding', 'local', 1536);
  h.submit();
  assert.equal(h.submitted[0].dimension, undefined);
  h.change('384');
  h.submit();
  assert.deepEqual(h.submitted[1], { slot: 'embedding', provider: 'local', dimension: 384 });
  h.change('invalid');
  assert.equal(h.nodes().find(n => n.props?.type === 'submit').props.disabled, true);
});

test('remote provider still requires an endpoint when no preset supplies it', () => {
  const h = form('embedding', 'custom');
  assert.equal(h.nodes().find(n => n.props?.type === 'submit').props.disabled, true);
  assert.equal(h.nodes().some(n => n.props?.type === 'password'), true);
});

test('revalidated provider-only local saves never resend a resolved remote width', async () => {
  const h = form('embedding', 'openai', 1536, true);
  h.pick('Offline identifier features');
  h.submit();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.submitted[0].dimension, undefined);
  h.refresh({ provider: 'local', native: true, dimension: 0 });
  h.submit();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.submitted[1].dimension, undefined);
  assert.equal(h.nodes().find(n => n.type === 'input').props.value, '');
});

test('a successful explicit vector edit becomes clean across refresh and repeat Save', async () => {
  const h = form('embedding', 'local', 1536, true);
  h.change('384');
  h.submit();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.submitted[0].dimension, 384);
  h.refresh({ provider: 'local', native: true, dimension: 384 });
  h.submit();
  assert.equal(h.submitted[1].dimension, undefined);
  assert.equal(h.nodes().find(n => n.type === 'input').props.value, '384');
});

test('verification reports disabled LLM honestly and labels local embeddings as identifier-only', async () => {
  const actions = compile('lib/actions/setup.ts', name => name === './gate' ? {
    mutate: async (_url, options) => ({ ok: true, message: options.success({
      llm: { provider: 'disabled', state: 'disabled' }, embedding: { provider: 'local', state: 'verified' },
    }) }),
  } : {});
  const result = await actions.verifyProviders();
  assert.match(result.message, /Language model intentionally disabled/);
  assert.match(result.message, /offline identifier features, not learned semantics/);
  assert.doesNotMatch(result.message, /failed/);
});
