const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript5');

const root = path.resolve(__dirname, '..');
const deferred = () => {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
};

// Exercise the actual form's handlers/effects with controlled async boundaries.
// This is a component contract test, not a rendered browser or live API test.
function formHarness() {
  const slots = [];
  let cursor = 0;
  let changed = false;
  let effects = [];
  let tree;
  const context = deferred();
  const probes = [];
  const submitted = [];
  const react = {
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = initial;
      return [slots[i], value => {
        const next = typeof value === 'function' ? value(slots[i]) : value;
        if (!Object.is(next, slots[i])) { slots[i] = next; changed = true; }
      }];
    },
    useRef(initial) {
      const i = cursor++;
      return slots[i] ??= { current: initial };
    },
    useMemo(fn) { cursor++; return fn(); },
    useEffect(fn, deps) {
      const i = cursor++;
      const previous = slots[i];
      if (!previous || !deps || deps.some((d, n) => !Object.is(d, previous.deps[n]))) {
        slots[i] = { deps, cleanup: previous?.cleanup };
        effects.push(() => { slots[i].cleanup?.(); slots[i].cleanup = fn(); });
      }
    },
  };
  const probeRemote = input => {
    const reply = deferred();
    probes.push({ input, ...reply });
    return reply.promise;
  };
  const connectRepository = async input => {
    submitted.push(input);
    return { ok: false, message: 'Synthetic contract boundary: not submitted to an API.' };
  };
  function compile(file, overrides = {}, suffix = '') {
    const output = ts.transpileModule(fs.readFileSync(path.join(root, file), 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
    }).outputText;
    const module = { exports: {} };
    vm.runInNewContext(output + suffix, { module, exports: module.exports, URL,
      require: name => Object.hasOwn(overrides, name) ? overrides[name] : require(name),
    }, { filename: file });
    return module.exports;
  }
  const { UrlForm } = compile('components/ConnectRepository.tsx', {
    react,
    'next/link': () => null,
    './act': { Dialog: () => null, LockNote: () => null,
      useAction: action => ({ run: action, pending: false, mode: 'on' }) },
    './AddProject': {}, './Capabilities': {}, './MemberScope': {}, './ui': { Chip: () => null },
    '@/lib/actions/git': { probeRemote, connectRepository },
    '@/lib/actions/projects': { projectRegistrationContext: () => context.promise },
    '@/lib/git-remote': compile('lib/git-remote.ts'),
  }, '\nexports.UrlForm = UrlForm;');
  function render() {
    for (let pass = 0; pass < 20; pass++) {
      cursor = 0; changed = false; effects = [];
      tree = UrlForm();
      effects.forEach(fn => fn());
      if (!changed) return tree;
    }
    throw new Error('Form did not settle');
  }
  function walk(node) {
    if (!node || typeof node !== 'object') return [];
    if (Array.isArray(node)) return node.flatMap(walk);
    return [node, ...walk(node.props?.children)];
  }
  const nodes = (node = tree) => walk(node);
  function find(predicate) {
    const node = nodes().find(predicate);
    assert.ok(node, 'Expected form control');
    return node;
  }
  const input = placeholder => find(n => n.type === 'input' && n.props.placeholder === placeholder);
  const checkbox = () => find(n => n.type === 'input' && n.props.type === 'checkbox');
  const change = (node, value) => { node.props.onChange({ target: { value, checked: value } }); render(); };
  const check = () => find(n => n.type === 'button' && typeof n.props.onClick === 'function').props.onClick();
  async function ready(data = { team: false, canIndex: true, projects: [] }) {
    context.resolve({ ok: true, data });
    await context.promise;
    render();
  }
  render();
  return { render, nodes, find, input, checkbox, change, check, ready, probes, submitted,
    submit: () => tree.props.onSubmit({ preventDefault() {} }) };
}

const urlInput = h => h.input('https://github.com/owner/repo.git');
const reachable = (branch = 'main') => ({ ok: true, message: 'Reachable.', data: {
  reachable: true, default_branch: branch, branches: [{ name: branch, head: 'abc' }],
} });

test('Connect keeps the index-after-cloning default when permissions finish loading', async () => {
  const h = formHarness();
  assert.equal(h.checkbox().props.disabled, true);
  await h.ready();
  assert.equal(h.checkbox().props.checked, true);
  h.change(urlInput(h), 'https://github.com/example/one.git');
  await h.submit();
  assert.equal(h.submitted[0].indexNow, true);
  h.change(h.checkbox(), false);
  await h.submit();
  assert.equal(h.submitted[1].indexNow, false);
});

test('a team project becoming indexable preserves the explicit index choice', async () => {
  const h = formHarness();
  await h.ready({ team: true, projects: [{ id: 'reader', name: 'Read only', canIndex: false }, { id: 'writer', name: 'Can run', canIndex: true }] });
  const project = () => h.find(n => n.type === 'select' && n.props.required);
  assert.equal(h.checkbox().props.checked, false);
  h.change(project(), 'writer');
  assert.equal(h.checkbox().props.checked, true);
  h.change(h.checkbox(), false);
  h.change(project(), 'reader');
  assert.equal(h.checkbox().props.disabled, true);
  h.change(project(), 'writer');
  assert.equal(h.checkbox().props.checked, false);
});

test('an old URL check cannot replace a new repository branch or status', async () => {
  const h = formHarness();
  await h.ready();
  h.change(urlInput(h), 'https://github.com/example/one.git');
  const pending = h.check();
  h.change(urlInput(h), 'https://github.com/example/two.git');
  h.probes[0].resolve(reachable('old-branch'));
  await pending;
  h.render();
  assert.equal(h.nodes().some(n => n.type === 'option' && n.props.value === 'old-branch'), false);
  await h.submit();
  assert.equal(h.submitted[0].url, 'https://github.com/example/two.git');
  assert.equal(h.submitted[0].branch, undefined);
});

test('changing credentials clears a check and ignores its late response', async () => {
  const h = formHarness();
  await h.ready();
  h.change(urlInput(h), 'https://github.com/example/one.git');
  const pending = h.check();
  h.change(h.input('Leave empty for a public repository'), 'synthetic-token');
  h.probes[0].resolve(reachable('old-auth-branch'));
  await pending;
  h.render();
  assert.equal(h.nodes().some(n => n.type === 'option'), false);
  const next = h.check();
  h.probes[1].resolve(reachable('authorized-branch'));
  await next;
  h.render();
  await h.submit();
  assert.equal(h.submitted[0].branch, 'authorized-branch');
  assert.equal(h.submitted[0].token, 'synthetic-token');
});

test('out-of-order checks retain only the latest response', async () => {
  const h = formHarness();
  await h.ready();
  h.change(urlInput(h), 'https://github.com/example/one.git');
  const first = h.check();
  const second = h.check();
  h.probes[1].resolve(reachable('newest'));
  await second;
  h.probes[0].resolve(reachable('older'));
  await first;
  h.render();
  await h.submit();
  assert.equal(h.submitted[0].branch, 'newest');
});

test('a probe without a default never visually selects an unsubmitted branch', async () => {
  const h = formHarness();
  await h.ready();
  h.change(urlInput(h), 'https://github.com/example/one.git');
  const pending = h.check();
  h.probes[0].resolve({ ok: true, message: 'Reachable.', data: { reachable: true, branches: [{ name: 'feature', head: 'abc' }] } });
  await pending;
  h.render();
  const select = h.find(n => n.type === 'select' && !n.props.required);
  assert.equal(select.props.value, '');
  assert.ok(h.nodes(select).some(n => n.type === 'option' && n.props.value === ''));
  h.change(select, 'feature');
  await h.submit();
  assert.equal(h.submitted[0].branch, 'feature');
});
