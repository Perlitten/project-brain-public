import "server-only";
import { apiConfigured, brainFetch } from "./api";
import { packContents, reportContents } from "./mock";

export type ArtifactKind = "packs" | "reports";
export interface Artifact { title: string; filename: string; content: string }

export function validArtifactId(kind: ArtifactKind, id: string): boolean {
  return kind === "packs" ? /^[1-9]\d{0,15}$/.test(id) : /^[^/\\\x00-\x1f]{1,200}\.md$/i.test(id) && id !== "..md";
}

export async function getArtifact(kind: ArtifactKind, id: string): Promise<Artifact | null> {
  if (!validArtifactId(kind, id)) return null;
  if (!apiConfigured) {
    const content = kind === "packs" ? packContents[id] : reportContents[id];
    if (!content) return null;
    const title = content.split("\n", 1)[0].replace(/^#\s*/, "") || (kind === "packs" ? `Context pack #${id}` : id);
    return { content, title, filename: kind === "packs" ? `context-pack-${id}.md` : id };
  }
  const path = kind === "packs" ? "context-packs" : "reports";
  const data = await brainFetch<{ content?: unknown; pack?: { task?: string }; report?: { title?: string } }>(`/api/web/${path}/${encodeURIComponent(id)}`, { fresh: true });
  if (!data || typeof data.content !== "string") return null;
  return { content: data.content, title: kind === "packs" ? data.pack?.task || `Context pack #${id}` : data.report?.title || id,
    filename: kind === "packs" ? `context-pack-${id}.md` : id };
}

/** Only the originating list is allowed as a back link. */
export function artifactBack(kind: ArtifactKind, value?: string): string {
  const fallback = `/${kind}`;
  if (!value) return fallback;
  try {
    const url = new URL(value, "https://brain.invalid");
    return url.origin === "https://brain.invalid" && url.pathname === fallback ? `${fallback}${url.search}` : fallback;
  } catch { return fallback; }
}
