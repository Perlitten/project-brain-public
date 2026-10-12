import type { MetadataRoute } from "next";

// Evaluated per request so the deployment's env, not the build machine's, decides.
export const dynamic = "force-dynamic";

/** Pure demo (no Brain API configured at all) may be crawled; anything else is private. */
export function isPureDemo() {
  return !process.env.BRAIN_API_URL?.trim() && !process.env.BRAIN_API_KEY?.trim();
}

export default function robots(): MetadataRoute.Robots {
  return isPureDemo() ? { rules: { userAgent: "*", allow: "/" } } : { rules: { userAgent: "*", disallow: "/" } };
}
