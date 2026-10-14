# Eval & Adversarial Verification

```bash
# Golden tasks against your target repository.
brain eval --repo <path-to-target-repo> --golden rules/golden_tasks.yaml

# Validate a golden task YAML file.
brain eval --repo <path-to-target-repo> --golden rules/golden_tasks.yaml --validate
```

### Embedding integrity

```bash
brain embeddings status
brain embeddings verify --json
brain embeddings backfill              # sync pgvector + regenerate incompatible
brain embeddings backfill --pgvector-only  # JSON -> pgvector only
```

### Proactive insights

```bash
# Queue a deterministic insight scan through the API.
curl -X POST http://localhost:8000/jobs/proactive-insights \
  -H "X-API-Key: $PROJECT_BRAIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'

# List stored insights.
curl http://localhost:8000/insights -H "X-API-Key: $PROJECT_BRAIN_API_KEY"
```

By default, insight scans use deterministic checks only. To let the low-latency
NVIDIA NIM route synthesize additional evidence-bound insights:

```text
DEFAULT_LLM_PROVIDER=nvidia
NVIDIA_API_KEY=<secret>
LLM_TASK_INSIGHT_MODEL=meta/llama-3.1-8b-instruct
PROACTIVE_INSIGHTS_LLM_ENABLED=true
```

The LLM stage is bounded by `PROACTIVE_INSIGHTS_MAX_SNAPSHOT_CHARS` and `PROACTIVE_INSIGHTS_TIMEOUT_S`. If the model fails or times out, deterministic insights still run and the failure is reported in the job result.

### External imports

```bash
# Grafify graph JSON -> Neo4j (uses GRAFIFY_OUTPUT_PATH or ../graphify-out/graph.json)
brain import grafify
brain import grafify --path ../graphify-out/graph.json

# Obsidian vault -> rules/decisions (requires OBSIDIAN_VAULT_PATH or --vault)
brain import obsidian --vault /path/to/vault
```

### Browser automation (optional)

Project Brain does **not** control Cursor's embedded Simple Browser or the OS desktop. For
optional local browser checks, a Playwright eval scaffold lives under `eval/browser_automation/`.

```bash
pip install -e ".[dev]"
playwright install chromium
python eval/browser_automation/smoke_test.py
```

Set `BROWSER_HEADLESS=false` in `.env` to watch the browser window. Platform limits (Computer
Use, Cursor UI, Puppeteer vs Playwright) are documented in [tooling gaps](tooling-gaps.md).

---

