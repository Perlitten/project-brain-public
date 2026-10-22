import type { ReadProblem as Problem } from "@/lib/read-problem";
import { RetryRead } from "./RetryRead";

export function ReadProblem({ problem }: { problem: Problem }) {
  return <div className="callout tone-warn" role="status">
    <b>{problem.title}</b><span>{problem.detail}</span>
    {problem.retry && <RetryRead />}
  </div>;
}
