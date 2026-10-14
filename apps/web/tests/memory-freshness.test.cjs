const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// Agents write decisions, rules, context packs, findings and reports while the
// dashboard is open. A cached read would hide a just-recorded decision for up to
// the revalidate window, so these endpoints must always be read fresh.
const MEMORY_ENDPOINTS = ['/decisions', '/rules', '/api/web/decisions', '/api/web/rules',
  '/api/web/context-packs', '/insights', '/api/web/reports', '/admin/principals'];

test('memory endpoints are fetched without caching', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'lib', 'data.ts'), 'utf8');
  const calls = source.split('\n').filter(line => line.includes('brainFetch<'));
  for (const endpoint of MEMORY_ENDPOINTS) {
    const hits = calls.filter(line => line.includes(`"${endpoint}`) || line.includes(`\`${endpoint}`));
    assert.ok(hits.length > 0, `no fetch found for ${endpoint}`);
    for (const line of hits) assert.match(line, /fresh: true/, `${endpoint} is cached: ${line.trim()}`);
  }
});
