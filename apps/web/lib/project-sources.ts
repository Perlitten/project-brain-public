import "server-only";
import { brainRead } from "./api";
import { getCapabilities } from "./capabilities";
export async function getRegisteredSources(): Promise<{ name: string; path: string; indexedRepositoryId: number | null }[] | null> {
  const capabilities = await getCapabilities();
  if (capabilities.state === "demo" || !capabilities.supported.includes("repository_registration")) return [];
  const reply = await brainRead<{ repositories?: { display_name?: string; canonical_root?: string; disabled?: boolean; indexed_repository_id?: number | null }[] }>("/workspace/repositories", { fresh: true });
  if (!reply.ok || !Array.isArray(reply.data.repositories)) return null;
  return reply.data.repositories.filter(r => !r.disabled && typeof r.canonical_root === "string").map(r => ({
    name: r.display_name || r.canonical_root!,
    path: r.canonical_root!,
    indexedRepositoryId: typeof r.indexed_repository_id === "number" ? r.indexed_repository_id : null,
  }));
}
