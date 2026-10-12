#!/usr/bin/env node
// `npm run dev` / `npm run start`: run Next bound to loopback unless told otherwise.
//
//   WEB_HOST   address to listen on (default 127.0.0.1). Anything that is not a
//              loopback address makes the dashboard reachable from other machines.
//   PORT       port (default 3100).
//
// When the address is loopback, nobody else can reach the dashboard, so a fresh
// install with no sign-in configured opens without a password (lib/web-auth.ts
// reads WEB_LOOPBACK_BIND=1). On any other address it stays locked until a
// sign-in exists, unless WEB_OPEN_MODE=true says otherwise.
import { spawn } from "node:child_process";
import { createRequire } from "node:module";

const mode = process.argv[2];
if (mode !== "dev" && mode !== "start") {
  console.error("usage: serve.mjs dev|start");
  process.exit(2);
}
const host = (process.env.WEB_HOST ?? "").trim() || "127.0.0.1";
const port = (process.env.PORT ?? "").trim() || "3100";
const loopback = host === "localhost" || host === "::1" || /^127(\.\d{1,3}){3}$/.test(host);
const env = { ...process.env };
if (loopback) env.WEB_LOOPBACK_BIND = "1";
else delete env.WEB_LOOPBACK_BIND;

const next = createRequire(import.meta.url).resolve("next/dist/bin/next");
const child = spawn(process.execPath, [next, mode, "--hostname", host, "--port", port, ...process.argv.slice(3)], { stdio: "inherit", env });
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => child.kill(signal));
child.on("exit", (code, signal) => process.exit(signal ? 1 : code ?? 0));
