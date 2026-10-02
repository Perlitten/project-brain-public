import type { ReactNode } from "react";
import { glossary, type GlossaryKey } from "@/lib/glossary";

// A jargon word that explains itself on hover, focus or tap. Pure CSS: the
// trigger is focusable, so keyboard and touch users get the same sentence.
export function Term({ k, children }: { k: GlossaryKey; children: ReactNode }) {
  return (
    <span className="term">
      <button type="button" className="term__word">
        {children}
      </button>
      <span className="term__tip" role="tooltip">
        {glossary[k]}
      </span>
    </span>
  );
}
