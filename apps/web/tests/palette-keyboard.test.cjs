const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript5');

function palette() {
  const calls = [];
  const states = [];
  let cursor = 0;
  const source = fs.readFileSync(path.resolve(__dirname, '../components/Shell.tsx'), 'utf8');
  const output = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  const exports = {};
  const requireModule = name => {
    if (name === 'react') return {
      useState: initial => {
        const i = cursor++;
        if (!(i in states)) states[i] = initial;
        return [states[i], value => { states[i] = typeof value === 'function' ? value(states[i]) : value; }];
      },
      useMemo: callback => callback(), useRef: () => ({ current: null }),
      useEffect() {}, useLayoutEffect() {},
    };
    if (name === 'react/jsx-runtime') return { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
    if (name === 'next/navigation') return { useRouter: () => ({ push: href => calls.push(href) }) };
    if (name === './MemberScope') return { useRestrictedMember: () => false };
    if (name === './Capabilities') return { useCapabilities: () => ({ supported: [] }) };
    if (name === '@/lib/nav') return { allNavItems: [
      { key: 'projects', label: 'Projects', hint: 'Repositories', href: '/projects' },
      { key: 'memory', label: 'Memory', hint: 'Decisions', href: '/memory' },
      { key: 'review', label: 'Memory review', hint: 'Proposals', href: '/review' },
    ] };
    return {};
  };
  vm.runInNewContext(output + '\nexports.Palette = Palette;', { exports, require: requireModule });
  const nodes = node => !node || typeof node !== 'object' ? [] : Array.isArray(node)
    ? node.flatMap(nodes) : [node, ...nodes(node.props?.children)];
  let input;
  const render = () => {
    cursor = 0;
    const tree = exports.Palette({ onClose: () => calls.push('close'), repos: [], withRepo: href => href,
      path: '/', mode: 'on', scoped: false });
    input = nodes(tree).find(n => n.type === 'input');
  };
  render();
  return { calls,
    search(value) { input.props.onChange({ target: { value } }); render(); },
    key(key) {
      let prevented = false;
      input.props.onKeyDown({ key, preventDefault: () => { prevented = true; calls.push(`prevent:${key}`); } });
      render();
      return prevented;
    },
  };
}

test('Enter consumes browser default before closing and navigating from the palette', () => {
  const p = palette();
  p.search('Projects');
  assert.equal(p.key('Enter'), true);
  assert.deepEqual(p.calls, ['prevent:Enter', 'close', '/projects']);
});

test('arrow selection followed by Enter navigates once and consumes the default activation', () => {
  const p = palette();
  p.search('Memory');
  assert.equal(p.key('ArrowDown'), true);
  assert.equal(p.key('Enter'), true);
  assert.deepEqual(p.calls, ['prevent:ArrowDown', 'prevent:Enter', 'close', '/review']);
});

test('Enter with no results still consumes default activation without closing', () => {
  const p = palette();
  p.search('no matching destination');
  assert.equal(p.key('Enter'), true);
  assert.deepEqual(p.calls, ['prevent:Enter']);
});

test('ordinary text input retains native keyboard behavior', () => {
  const p = palette();
  assert.equal(p.key('p'), false);
  assert.deepEqual(p.calls, []);
});

test('ArrowUp at the first entry stays bounded', () => {
  const p = palette();
  assert.equal(p.key('ArrowUp'), true);
  p.key('Enter');
  assert.equal(p.calls.at(-1), '/projects');
  assert.equal(p.calls.filter(x => x === 'close').length, 1);
});
