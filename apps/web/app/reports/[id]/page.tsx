import { notFound } from "next/navigation";
import { ArtifactReader } from "@/components/ArtifactReader";
import { artifactBack, getArtifact, validArtifactId } from "@/lib/artifacts";

export const metadata = { title: "Report" };
export default async function Report({ params, searchParams }: { params: Promise<{ id: string }>; searchParams: Promise<{ back?: string }> }) {
  const { id } = await params;
  if (!validArtifactId("reports", id)) notFound();
  const [artifact, query] = await Promise.all([getArtifact("reports", id), searchParams]);
  return <ArtifactReader kind="reports" id={id} artifact={artifact} back={artifactBack("reports", query.back)} />;
}
