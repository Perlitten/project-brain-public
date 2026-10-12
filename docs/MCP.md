# MCP Server — protocol details

### Protocol features

Besides tools, both servers implement:

* **Tool annotations.** Every tool carries a title and read-only, destructive,
  idempotent and open-world hints, so clients can auto-approve the read-only ones.
* **Resources.** `brain://rules` and `brain://decisions` list the active rules and
  decisions; `brain://payload/{token}` holds the tail of oversized tool results.
* **Prompts.** `plan_change`, `review_changes` and `capture_lesson` encode the
  intended tool sequence for common jobs.
* **Elicitation.** With `BRAIN_MCP_CONFIRM_WRITES=true`, `record_decision` and
  `record_rule` ask the human to approve the write when the client supports
  elicitation. It is opt-in, because an unattended agent would stall on the prompt.
* **Sampling.** When the server-side model fails, the remote `ask_project` asks the
  client's own model to answer from the Brain's context pack instead of returning
  an error.

### MCP stdio E2E verification

```bash
python eval/mcp_stdio_e2e.py
# writes reports/mcp-stdio-e2e.json
```

### Protocol rules for AI Agents (Antigravity / Claude Code / Codex)
Agents must respect these rules prior to modifying the main repository:
1. **Before writing code**: Invoke `prepare_task_context` to request the relevant files, tests, and architectural constraints.
2. **Read the Context Pack**: Analyze the generated context pack before initiating changes.
3. **Minimize footprint**: Do not modify files outside the context pack scope.
4. **After editing**: Invoke `review_diff` to run checks on generated diffs, checking for rule violations or untested files.

---

