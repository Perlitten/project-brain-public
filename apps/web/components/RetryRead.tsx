"use client";

import { useTransition } from "react";
import { useRouter } from "next/navigation";

/** Refreshes read data only; never repeats a submitted mutation. */
export function RetryRead() {
  const router = useRouter();
  const [pending, start] = useTransition();
  return <button type="button" className="btn btn--neutral btn--sm" disabled={pending} onClick={() => start(() => router.refresh())}>{pending ? "Checking…" : "Retry"}</button>;
}
