import "server-only";
import { getArtifact, type ArtifactKind } from "./artifacts";
import { authorizeWebRequest, sessionFrom, webAuthResponse } from "./web-auth";

export async function downloadArtifact(request: Request, kind: ArtifactKind, id: string): Promise<Response> {
  const authorization = authorizeWebRequest(request.headers.get("authorization"), sessionFrom(request.headers.get("cookie")));
  if (authorization !== 200) return webAuthResponse(authorization);
  const artifact = await getArtifact(kind, id);
  if (!artifact) return new Response("Content unavailable", { status: 404, headers: { "Cache-Control": "private, no-store" } });
  return new Response(artifact.content, { headers: { "Content-Type": "text/markdown; charset=utf-8", "Cache-Control": "private, no-store",
    "Content-Disposition": `attachment; filename="${kind === "packs" ? `context-pack-${id}.md` : "report.md"}"; filename*=UTF-8''${encodeURIComponent(artifact.filename)}` } });
}
