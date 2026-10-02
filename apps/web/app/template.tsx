// Remounts on every navigation, so each screen enters with the same
// choreography: sections rise in sequence (see "Хореография" in docs/BRAND.md).
export default function Template({ children }: { children: React.ReactNode }) {
  return <div className="route">{children}</div>;
}
