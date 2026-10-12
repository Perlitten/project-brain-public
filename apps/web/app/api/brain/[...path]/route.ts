import type { NextRequest } from "next/server";
import { authorizeWebRequest, lockReason, sessionFrom, webAuthResponse } from "@/lib/web-auth";

// Server-side bridge to the Brain API on the VPS. The API key lives only in
// the Vercel environment and is attached here, so it never reaches a browser.
// Read-only until actions are designed with explicit confirmation.
// Nothing is forwarded with the server key unless authorizeWebRequest lets
// the caller in; every refusal is JSON {error, detail}.
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const authorization = await authorizeWebRequest(req.headers.get("authorization"), sessionFrom(req.headers.get("cookie")), req.headers.get("host"));
  if (authorization !== 200) return webAuthResponse(authorization, authorization === 503 ? await lockReason(req.headers.get("host")) : null);
  const base = process.env.BRAIN_API_URL;
  const key = process.env.BRAIN_API_KEY;
  if (!base || !key) {
    return Response.json(
      { error: "not_configured", detail: "Set BRAIN_API_URL and BRAIN_API_KEY in the deployment environment." },
      { status: 503 },
    );
  }
  const { path } = await ctx.params;
  const target = new URL(`api/${path.map(encodeURIComponent).join("/")}`, base.endsWith("/") ? base : `${base}/`);
  target.search = req.nextUrl.search;
  try {
    const upstream = await fetch(target, {
      headers: { "X-API-Key": key, Accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("Content-Type") ?? "application/json" },
    });
  } catch {
    return Response.json(
      { error: "brain_unreachable", detail: "The Brain API did not answer. Check that it is running and that BRAIN_API_URL is right." },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
}
