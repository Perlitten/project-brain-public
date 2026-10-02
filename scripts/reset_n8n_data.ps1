# Reset n8n SQLite volume (removes all imported workflows and credentials).
# Use after removing legacy External Brain workflow exports from the repo.
# Requires: Docker Compose stack defined in project root.

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "Stopping n8n container..."
docker compose stop n8n

Write-Host "Removing n8n container..."
docker compose rm -f n8n

$volume = (docker volume ls --format "{{.Name}}" | Select-String "n8n_data" | Select-Object -First 1)
if ($volume) {
    Write-Host "Removing volume $($volume.Line)..."
    docker volume rm $volume.Line
} else {
    Write-Host "No n8n_data volume found (already clean)."
}

Write-Host "Starting n8n..."
docker compose up -d n8n

Write-Host ""
Write-Host "Done. Re-import Project Brain workflows from n8n/workflows/:"
Write-Host "  - git-merge-reindex.json"
Write-Host "  - nightly-health.json"
Write-Host "  - weekly-benchmark.json"
Write-Host "  - nightly-proactive-insights.json"
