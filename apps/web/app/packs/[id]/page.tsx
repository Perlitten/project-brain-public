import { notFound } from "next/navigation";
import { ArtifactReader } from "@/components/ArtifactReader";
import { artifactBack, getArtifact, validArtifactId } from "@/lib/artifacts";

export const metadata = { title: "Context pack" };
export default async function Pack({ params, searchParams }: { params: Promise<{ id: string }>; searchParams: Promise<{ back?: string }> }) {
  const { id } = await params;
  if (!validArtifactId("packs", id)) notFound();
  const [artifact, query] = await Promise.all([getArtifact("packs", id), searchParams]);
  return <ArtifactReader kind="packs" id={id} artifact={artifact} back={artifactBack("packs", query.back)} />;
}
