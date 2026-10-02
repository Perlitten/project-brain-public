#!/usr/bin/env bash
# Self-heal the dedicated Project Brain n8n container when it is stopped or
# unhealthy. Docker healthchecks only report status; they do not restart unhealthy
# containers by themselves.
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE="${COMPOSE:-docker compose -f docker-compose.prod.yml}"
ENV_FILE=".env"

n8n_port="$(grep -E '^BRAIN_N8N_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)"
n8n_port="${n8n_port:-5680}"

if ! $COMPOSE ps --status running --services | grep -Fxq n8n; then
    echo "brain-n8n is not running; starting n8n"
    $COMPOSE up -d n8n
    exit 0
fi

health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' brain-n8n 2>/dev/null || echo missing)"
case "$health" in
    healthy)
        ;;
    starting)
        echo "brain-n8n health is still starting; leaving it alone"
        exit 0
        ;;
    *)
        echo "brain-n8n health is ${health}; restarting n8n"
        $COMPOSE restart n8n
        exit 0
        ;;
esac

if ! curl -fsS "http://127.0.0.1:${n8n_port}/healthz" >/dev/null 2>&1; then
    echo "brain-n8n /healthz failed on 127.0.0.1:${n8n_port}; restarting n8n"
    $COMPOSE restart n8n
fi
