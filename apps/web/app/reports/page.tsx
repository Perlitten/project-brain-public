import { redirect } from "next/navigation";

// The saved-report list moved onto Quality as a tab, next to the benchmark it
// is evidence for. The old address keeps working — including bookmarks that
// carry a search or page — because report detail pages still live under
// /reports/<id> and link back here.
export default async function Reports({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const query = new URLSearchParams({ view: "reports" });
  for (const [key, value] of Object.entries(params)) {
    if (key === "view") continue;
    if (typeof value === "string") query.set(key, value);
  }
  redirect(`/quality?${query}`);
}
