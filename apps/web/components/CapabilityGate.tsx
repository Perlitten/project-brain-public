import { getCapabilities } from "@/lib/capabilities";
import type { Capability } from "@/lib/capability-contract";
import { EmptyState, PageHead } from "./ui";
import { RetryRead } from "./RetryRead";

export async function capabilityGate(name: Capability, title: string) {
  const features = await getCapabilities();
  if (features.supported.includes(name)) return null;
  return <><PageHead title={title} /><EmptyState title={features.state === "unavailable" ? "Server capabilities unavailable" : "Not supported by this server"}
    body={features.state === "unavailable" ? "Feature discovery failed. Retry before changing the server or dashboard configuration." : "This API does not advertise support for this feature. Update the Brain server before using it."}
    action={<RetryRead />} /></>;
}
