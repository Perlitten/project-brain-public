const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript5');
const root = path.resolve(__dirname, '..');

// Compile lib/team-notifications-view.ts with the Brain API boundary replaced.
function loadView(brainRead) {
  const filename = path.join(root, 'lib', 'team-notifications-view.ts');
  const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const module = { exports: {} };
  const requireModule = (name) => {
    if (name === 'server-only') return {};
    // These tests cover the live mapping, so the API counts as configured and the demo data is not loaded.
    if (name === './api') return { brainRead, apiConfigured: true };
    if (name === './team-demo') return {};
    return require(name);
  };
  vm.runInNewContext(output, { module, exports: module.exports, require: requireModule, Number }, { filename });
  return module.exports;
}

test('the inbox reads only the caller\'s own notifications and drops malformed rows', async () => {
  const calls = [];
  const view = loadView(async (p) => {
    calls.push(p);
    return {
      ok: true,
      data: {
        team_state: 'team',
        unread_count: 1,
        notifications: [
          { id: 7, kind: 'memory.review_needed', summary: 'Decision proposal #3 awaits your review in A', proposal_id: 3, proposal_state: 'pending', created_at: '2026-10-18T10:00:00+00:00', read_at: null },
          { id: 'x', kind: 'memory.review_needed' },
          { id: 6, kind: 'memory.review_needed', proposal_id: 2, proposal_state: 'approved', read_at: '2026-10-18T11:00:00+00:00' },
        ],
      },
    };
  });
  const inbox = await view.getNotifications();
  assert.deepEqual(calls, ['/team/me/notifications?limit=50']);
  assert.equal(inbox.kind, 'ok');
  assert.equal(inbox.unread, 1);
  assert.deepEqual(inbox.rows.map((r) => [r.id, r.proposalState, Boolean(r.readAt)]), [[7, 'pending', false], [6, 'approved', true]]);
  assert.equal(inbox.rows[1].summary, 'memory.review_needed', 'a missing summary falls back to the kind');
});

test('a refusal or an old server is "unavailable", never an empty inbox', async () => {
  const refused = loadView(async () => ({ ok: false, status: 403, reason: 'http_error' }));
  assert.equal((await refused.getNotifications()).kind, 'unavailable');
  assert.equal((await refused.getTelegramLink()).kind, 'unavailable');
  const old = loadView(async () => ({ ok: true, data: { detail: 'Not Found' } }));
  assert.equal((await old.getNotifications()).kind, 'unavailable');
  assert.equal((await old.getTelegramLink()).kind, 'unavailable');
});

test('the Telegram link view keeps the masked chat and the pending state', async () => {
  const view = loadView(async () => ({
    ok: true,
    data: { team_state: 'team', available: true, linked: true, chat: '…6789', linked_at: '2026-10-18T10:00:00+00:00', pending: true, pending_chat: '…4321', code_expires_at: '2026-10-18T10:10:00+00:00' },
  }));
  const link = await view.getTelegramLink();
  assert.equal(link.kind, 'ok');
  assert.equal(link.available, true);
  assert.equal(link.linked, true);
  assert.equal(link.chat, '…6789');
  assert.equal(link.pending, true);
  assert.equal(link.pendingChat, '…4321');
  const solo = view.toTelegramLink({ team_state: 'solo', available: false, linked: false });
  assert.equal(solo.teamState, 'solo');
  assert.equal(solo.available, false);
  assert.equal(solo.chat, null);
});
