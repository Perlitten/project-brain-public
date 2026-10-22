// Run after `npm run build`. The same unbound build must support runtime API
// bindings without returning cached example content as live project data.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const { existsSync } = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");

async function listen(server) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return server.address().port;
}

async function startWeb(bindings) {
  const reservation = http.createServer();
  const port = await listen(reservation);
  await new Promise((resolve) => reservation.close(resolve));
  const child = spawn(process.execPath, ["scripts/serve.mjs", "start"], {
    cwd: root,
    env: { ...process.env, PORT: String(port), WEB_HOST: "127.0.0.1", WEB_BASIC_AUTH: "",
      WEB_OPEN_MODE: "", BRAIN_WEB_ACTIONS: "", BRAIN_DEMO_MODE: "", BRAIN_API_URL: "", BRAIN_API_KEY: "", ...bindings },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let log = "";
  child.stdout.on("data", (b) => { log += b.toString(); });
  child.stderr.on("data", (b) => { log += b.toString(); });
  const base = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 100; i++) {
    if (child.exitCode !== null) throw new Error(`Server exited: ${log}`);
    try {
      await fetch(`${base}/robots.txt`, { signal: AbortSignal.timeout(500) });
      return { child, base };
    } catch { await new Promise((resolve) => setTimeout(resolve, 100)); }
  }
  child.kill("SIGTERM");
  throw new Error(`Server did not start: ${log}`);
}

async function stopWeb(server) {
  const exit = once(server.child, "exit");
  server.child.kill("SIGTERM");
  await exit;
}

test("one production build serves demo or actual API data from runtime configuration", { timeout: 60000 }, async () => {
  assert.ok(existsSync(path.join(root, ".next", "BUILD_ID")), "Run npm run build first");
  const calls = [];
  const key = "production-integration-test-only";
  const marker = "runtime-bound-repository";
  const backend = http.createServer((req, res) => {
    calls.push({ path: req.url, key: req.headers["x-api-key"] });
    res.setHeader("Content-Type", "application/json");
    if (req.headers["x-api-key"] !== key) { res.writeHead(401); return res.end("{}"); }
    const payload = req.url === "/api/web-login" ? { mode: "open", version: "0", from_env: [] }
      : req.url === "/repositories" ? { repositories: [{ id: 1, name: marker, path: "/fixture", indexing_status: "ready" }] }
      : {};
    res.end(JSON.stringify(payload));
  });
  const backendPort = await listen(backend);
  let demo, live;
  try {
    demo = await startWeb({});
    let response = await fetch(`${demo.base}/projects`);
    assert.equal(response.status, 200);
    const demoHtml = await response.text();
    assert.match(demoHtml, /Demo data · synthetic examples/);
    assert.doesNotMatch(demoHtml, new RegExp(marker));
    assert.match(await (await fetch(`${demo.base}/robots.txt`)).text(), /Allow: \//);
    await stopWeb(demo); demo = null;

    live = await startWeb({ BRAIN_API_URL: `http://127.0.0.1:${backendPort}`, BRAIN_API_KEY: key });
    response = await fetch(`${live.base}/projects`);
    assert.equal(response.status, 200);
    const liveHtml = await response.text();
    assert.match(liveHtml, new RegExp(marker), "Must render the runtime backend, not the build-time demo");
    assert.doesNotMatch(liveHtml, /Demo data · synthetic examples/);
    assert.ok(!liveHtml.includes(key), "Server-side API credentials must not enter the page");
    assert.doesNotMatch(liveHtml, /Numbers are examples until the API is connected/);
    assert.ok(calls.some((c) => c.path === "/repositories" && c.key === key));
    assert.match(await (await fetch(`${live.base}/robots.txt`)).text(), /Disallow: \//);
  } finally {
    if (demo) await stopWeb(demo);
    if (live) await stopWeb(live);
    await new Promise((resolve) => backend.close(resolve));
  }
});


test("isolated demo misconfiguration cannot read from a backend even with valid operator auth", { timeout: 60000 }, async () => {
  let calls = 0;
  const backend = http.createServer((req, res) => { calls++; res.end("{}"); });
  const port = await listen(backend);
  let server;
  try {
    server = await startWeb({ BRAIN_DEMO_MODE: "1", BRAIN_API_URL: `http://127.0.0.1:${port}`,
      BRAIN_API_KEY: "demo-safety-fixture", WEB_BASIC_AUTH: "fixture:fixture" });
    const headers = { Authorization: `Basic ${Buffer.from("fixture:fixture").toString("base64")}` };
    for (const route of ["/projects", "/api/brain/setup/status"]) {
      const response = await fetch(server.base + route, { headers });
      assert.equal(response.status, 503);
      assert.doesNotMatch(await response.text(), /demo-safety-fixture/);
    }
    assert.equal(calls, 0, "No operator API request is permitted in isolated demo mode");
  } finally {
    if (server) await stopWeb(server);
    await new Promise((resolve) => backend.close(resolve));
  }
});

test("production pages distinguish failure, missing source, unmeasured tools and first-admin preparation", { timeout: 60000 }, async () => {
  let setupStatus = 503;
  const backend = http.createServer((req, res) => {
    res.setHeader("Content-Type", "application/json");
    let data = {};
    if (req.url === "/api/web-login") data = { mode: "open", version: "0", from_env: [] };
    else if (req.url === "/api/setup/status") {
      if (setupStatus !== 200) { res.writeHead(setupStatus); return res.end('{}'); }
      data = { steps: ['services', 'repo', 'provider', 'indexed', 'agent', 'first_task'].map(id => ({ id, status: 'done', detail: 'configured' })) };
    } else if (req.url === "/repositories") data = { repositories: [{ id: 1, name: "missing-checkout", path: "/repos/example", freshness: { status: "source_missing", commits_behind: 8 } }] };
    else if (req.url === "/api/status/mcp") data = { available: true, tools: [
      { name: "search_code", calls_24h: 1, error_rate: 0, p95_ms: 3300 },
      { name: "unused_tool", calls_24h: 0, error_rate: 0 },
    ] };
    else if (["/api/web/dashboard", "/api/web/agent-value", "/api/web/quality"].some(path => req.url.split("?")[0] === path)) { res.writeHead(503); return res.end('{}'); }
    else if (req.url.startsWith("/team/activity")) { res.writeHead(403); return res.end('{}'); }
    else if (req.url === "/team/me") data = { principal: { id: 1, name: "dashboard", kind: "service" }, team_state: "solo", scopes: [], projects: [] };
    res.end(JSON.stringify(data));
  });
  const port = await listen(backend);
  let web;
  try {
    web = await startWeb({ BRAIN_API_URL: `http://127.0.0.1:${port}`, BRAIN_API_KEY: "synthetic-dashboard-key" });
    let html = await (await fetch(web.base + "/setup")).text();
    assert.match(html, /Brain returned HTTP 503/);
    assert.doesNotMatch(html, /docker compose up/);
    setupStatus = 403;
    html = await (await fetch(web.base + "/setup")).text();
    assert.match(html, /Permission required/);
    setupStatus = 200;
    html = await (await fetch(web.base + "/setup")).text();
    assert.match(html, /Brain is fully set up/);
    html = await (await fetch(web.base + "/projects")).text();
    assert.match(html, /1 source unavailable/);
    assert.match(html, /mounted at the registered path/);
    assert.doesNotMatch(html, /1 of 1 projects are behind/);
    html = await (await fetch(web.base + "/")).text();
    assert.match(html, /1 projects: 1 source unavailable/);
    assert.match(html, /Check source for missing-checkout/);
    assert.match(html, /href="\/projects\?repo=1#source"/);
    assert.doesNotMatch(html, /projects need attention/);
    for (const route of ['/graph?repo=1', '/indexing?repo=1']) {
      html = await (await fetch(web.base + route)).text();
      assert.match(html, /aria-label="Repository filter"/);
      assert.match(html, /missing-checkout/);
    }
    const redirected = await fetch(web.base + '/logs?repo=1&period=24h', { redirect: 'manual' });
    assert.equal(redirected.status, 307);
    assert.match(redirected.headers.get('location'), /\/activity\?repo=1&period=24h$/);
    for (const [route, title] of [['/activity', 'Activity'], ['/memory', 'Memory']]) {
      html = await (await fetch(web.base + route)).text();
      assert.match(html, new RegExp(`<title>${title} · Project Brain</title>`));
      const heading = html.match(/<h1\b[^>]*>([\s\S]*?)<\/h1>/)?.[1].replace(/<[^>]*>/g, '').replace(/&amp;/g, '&');
      assert.equal(heading, title);
      assert.match(html, new RegExp(`crumb__page">${title}</span>`));
    }
    html = await (await fetch(web.base + "/mcp")).text();
    assert.match(html, /1 tool slow or failing; 1 of 2 used/);
    assert.doesNotMatch(html, /All agent tools are healthy/);
    html = await (await fetch(web.base + "/team/activity")).text();
    assert.match(html, /Permission required/);
    assert.doesNotMatch(html, /id="activity-filters"/);
    html = await (await fetch(web.base + "/team")).text();
    assert.match(html, /Prepare administrator key/);
    assert.match(html, /Shared sign-in stays active until the final step/);
    assert.ok(!html.includes("synthetic-dashboard-key"));
  } finally {
    if (web) await stopWeb(web);
    await new Promise(resolve => backend.close(resolve));
  }
});
