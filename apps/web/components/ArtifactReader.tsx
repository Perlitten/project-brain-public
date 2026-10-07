import Link from "next/link";
import { EmptyState, PageHead, Panel } from "./ui";
import type { Artifact, ArtifactKind } from "@/lib/artifacts";

export function ArtifactReader({ kind, id, artifact, back }: { kind: ArtifactKind; id: string; artifact: Artifact | null; back: string }) {
  return <>
    <PageHead eyebrow={kind === "packs" ? "Context packs" : "Reports"} title={artifact?.title ?? "Content unavailable"}
      lede={artifact ? "Complete saved Markdown, including all files and evidence." : "Brain could not return this saved content. It may have been removed, or the service may be unavailable."}
      actions={<><Link className="btn btn--neutral" href={back}>Back to list</Link>
        {artifact && <a className="btn btn--primary" href={`/${kind}/${encodeURIComponent(id)}/download`} download={artifact.filename}>Download Markdown</a>}</>} />
    <Panel id="artifact-content" title="Saved content">
      {artifact ? <pre className="artifact-content">{artifact.content}</pre> : <EmptyState title="No content returned" body="Return to the list and try again once Brain is available." />}
    </Panel>
  </>;
}
