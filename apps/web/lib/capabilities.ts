import "server-only";
import { cache } from "react";
import { apiConfigured, brainRead } from "./api";
import { CAPABILITY_NAMES, type Capability, type Capabilities } from "./capability-contract";

/** Request-local: feature discovery never caches another member's responses. */
export const getCapabilities = cache(async (): Promise<Capabilities> => {
  if (!apiConfigured) return { state: "demo", supported: [...CAPABILITY_NAMES] };
  const result = await brainRead<{ capabilities?: Partial<Record<Capability, unknown>> }>("/api/version", { fresh: true });
  if (!result.ok) return { state: "unavailable", supported: [] };
  if (!result.data || typeof result.data !== "object" || Array.isArray(result.data)) return { state: "unavailable", supported: [] };
  if (!result.data.capabilities) return { state: "legacy", supported: [] };
  return { state: "live", supported: CAPABILITY_NAMES.filter(name => result.data.capabilities?.[name] === true) };
});
