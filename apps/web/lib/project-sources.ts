import "server-only";
import { brainRead } from "./api";
import { getCapabilities } from "./capabilities";
export async function getRegisteredSources(): Promise<{ name: string; path: string }[] | null> {
  if (!(await getCapabilities()).supported.includes("repository_registration")) return [];
  const reply = await brainRead<{ repositories?: { display_name?: string; canonical_root?: string; disabled?: boolean }[] }>("/workspace/repositories", { fresh: true });
  if (!reply.ok || !Array.isArray(reply.data.repositories)) return null;
  return reply.data.repositories.filter(r => !r.disabled && typeof r.canonical_root === "string").map(r => ({ name: r.display_name || r.canonical_root!, path: r.canonical_root! }));
}
