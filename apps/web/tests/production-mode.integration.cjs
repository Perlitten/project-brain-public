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
