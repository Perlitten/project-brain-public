import type { ReactNode } from "react";
import { glossary, type GlossaryKey } from "@/lib/glossary";
import { Tooltip } from "./Tooltip";

// A jargon word that explains itself on hover, focus or tap. The shared
// portal keeps its explanation visible outside scrolling panels.
export function Term({ k, children }: { k: GlossaryKey; children: ReactNode }) {
  return <Tooltip content={glossary[k]}>{children}</Tooltip>;
}
