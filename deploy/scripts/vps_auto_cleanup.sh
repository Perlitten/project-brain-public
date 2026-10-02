#!/usr/bin/env bash
# ==============================================================================
# VPS Automated Garbage & Backup Pruning Daemon for Project Brain
# Prevents disk clutter from daily backup accumulation, docker build layers,
# container log growth, and build cache accumulation.
# ==============================================================================
set -euo pipefail

LOG_FILE="/var/log/project-brain-cleanup.log"
exec > >(tee -a "${LOG_FILE}") 2>&1

# Resolve the operator home from the installation location by default.  CI and
# alternate hosts can override it explicitly without baking a user name into
# the release artifact.
BRAIN_USER_HOME="${PROJECT_BRAIN_USER_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"

echo "=============================================================================="
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Starting Automated VPS Disk Cleanup"
echo "=============================================================================="

# 1. Prune old Hermes backups (keep last 2)
if [ -d "/backup/hermes" ]; then
    echo "==> Pruning /backup/hermes (retaining last 2 backups)..."
    cd /backup/hermes
    ls -t *.tar.gz 2>/dev/null | tail -n +3 | xargs -r rm -f || true
fi

# 2. Prune old root Postgres backups (keep last 2)
if [ -d "/backup/postgres" ]; then
    echo "==> Pruning /backup/postgres (retaining last 2 backups)..."
    cd /backup/postgres
    ls -t pg5433_*.sql.gz 2>/dev/null | tail -n +3 | xargs -r rm -f || true
    ls -t postgres_*.sql.gz 2>/dev/null | tail -n +3 | xargs -r rm -f || true
fi

# 3. Prune old Brain daily backups (keep last 2)
if [ -d "${BRAIN_USER_HOME}/backups/brain" ]; then
    echo "==> Pruning ${BRAIN_USER_HOME}/backups/brain (retaining last 2 backups)..."
    cd "${BRAIN_USER_HOME}/backups/brain"
    ls -t brain-*.sql.gz 2>/dev/null | tail -n +3 | xargs -r rm -f || true
fi

# 4. Prune Docker build cache (older than 48 hours) and dangling images
echo "==> Pruning Docker build cache and dangling images..."
docker builder prune -a -f --filter "until=48h" || true
docker image prune -f || true

# 5. Truncate Docker container logs larger than 50MB
echo "==> Truncating Docker container JSON logs..."
for log in /var/lib/docker/containers/*/*-json.log; do
    if [ -f "$log" ]; then
        sz_mb=$(du -m "$log" | cut -f1)
        if [ "$sz_mb" -gt 50 ]; then
            echo "Truncating large container log (${sz_mb} MB): $log"
            truncate -s 0 "$log"
        fi
    fi
done

# 6. Clean Snap cache and Gradle build caches
echo "==> Clearing Snap cache and Gradle build caches..."
rm -rf /var/lib/snapd/cache/* || true
rm -rf "${BRAIN_USER_HOME}/actions-runner/.gradle/caches" || true
rm -rf "${BRAIN_USER_HOME}/.npm/_npx" || true

# 7. Vacuum systemd journal logs to 100M
echo "==> Vacuuming systemd journal logs..."
journalctl --vacuum-size=100M || true

# 8. Clean apt cache
echo "==> Cleaning apt package cache..."
apt-get clean || true

echo "=============================================================================="
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Cleanup Completed successfully!"
df -h /
echo "=============================================================================="
