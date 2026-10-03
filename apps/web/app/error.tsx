"use client";
// A screen that crashed says so and offers the two things that help: try
// again, or go back to the overview.
import Link from "next/link";

export default function ScreenError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <section className="panel panel--spine panel--bad">
      <div className="panel__body form">
        <p className="eyebrow">Something broke</p>
        <h1 className="page-head__title">This screen couldn’t load</h1>
        <p className="text-dim">
          The dashboard hit an error while building this page. Brain itself is probably fine — try again, and if it keeps
          happening, check the dashboard server logs{error.digest ? ` for error ${error.digest}` : ""}.
        </p>
        <div className="btn-row">
          <button type="button" className="btn btn--primary" onClick={reset}>
            Try again
          </button>
          <Link className="btn btn--neutral" href="/">
            Go to the overview
          </Link>
        </div>
      </div>
    </section>
  );
}
