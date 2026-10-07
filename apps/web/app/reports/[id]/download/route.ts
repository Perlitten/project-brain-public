import { downloadArtifact } from "@/lib/artifact-download";

export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return downloadArtifact(request, "reports", id);
}
