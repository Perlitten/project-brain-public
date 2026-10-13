# Starter contribution ideas

Choose a small, reproducible improvement and open an issue before a larger
change. These are contribution directions, not claims of confirmed defects or
already-published GitHub issues. Check current code and existing issues first.

1. **Platform verification.** Run the complete [source setup](GETTING_STARTED.md)
   on macOS or WSL2 in a clean environment. Record versions, actual commands,
   failures and MCP results. Update support claims only with evidence; native
   Windows needs a separate installation design and verification.
2. **Keyboard and assistive-technology checks.** Exercise setup, navigation,
   forms and the code map using keyboard-only navigation and a screen reader.
   Report concrete focus/label problems and add focused regression coverage.
   Use the [brand and interaction contract](BRAND.md).
3. **Evaluation onboarding.** Improve a narrow portion of [Evaluation](EVAL.md)
   with a verified, repeatable example. Keep mock smoke checks separate from
   real model relevance and coding-outcome measurements; preserve baselines.
4. **MCP client examples.** Reproduce the [first MCP task](GETTING_STARTED.md#connect-mcp-and-run-one-task)
   in a client not yet verified. Record the client version and actual tool calls;
   keep configuration secret-free and use canonical repository scope.
5. **Failure recovery documentation.** Reproduce a safe service interruption in
   a disposable instance, then improve the relevant troubleshooting example.
   Avoid operator data and verify restart plus a representative tool request.

[Contribution guide](../CONTRIBUTING.md) describes checks and pull requests.
Do not weaken authentication or quality gates to complete a starter task.
