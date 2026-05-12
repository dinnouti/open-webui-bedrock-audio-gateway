#!/usr/bin/env bash
set -euo pipefail

# Run smoke tests against the Docker Compose service.
# Builds, starts, waits for healthy, runs tests, then tears down.

cd "$(dirname "$0")/.."

PORT="${GATEWAY_PORT:-8000}"

echo "Building and starting Docker Compose..."
docker compose build --quiet
docker compose up -d

trap 'echo ""; echo "Tearing down..."; docker compose down' EXIT

# Wait for healthy status
echo "Waiting for container to become healthy..."
for i in $(seq 1 60); do
    STATUS=$(docker compose ps --format json 2>/dev/null | python3 -c "
import sys, json
for line in sys.stdin:
    obj = json.loads(line)
    health = obj.get('Health', '')
    if health == 'healthy':
        print('healthy')
        break
" 2>/dev/null || echo "")
    if [ "$STATUS" = "healthy" ]; then
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "Container failed to become healthy. Logs:"
        docker compose logs --tail 30
        exit 1
    fi
    sleep 1
done

echo "Container healthy"
echo ""

./tests/smoke_test.sh "http://localhost:$PORT"
