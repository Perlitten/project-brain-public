// Shown while a screen waits for the Brain API, so a slow answer reads as
// "loading", not as a frozen page.
export default function Loading() {
  return (
    <div className="loading" aria-busy="true" aria-label="Loading">
      <span className="skeleton" style={{ width: "18%" }} />
      <span className="skeleton skeleton--title" />
      <span className="skeleton" style={{ width: "56%" }} />
      <span className="skeleton skeleton--block" />
      <span className="skeleton skeleton--block" />
    </div>
  );
}
