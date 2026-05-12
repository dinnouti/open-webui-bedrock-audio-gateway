#!/usr/bin/env bash
set -euo pipefail

# Run smoke tests against a locally-started server.
# Starts the server, waits for health, runs tests, then stops the server.

cd "$(dirname "$0")/.."

PORT="${GATEWAY_PORT:-8000}"
LOG="/tmp/audio-gateway-test.log"

echo "Starting local server on port $PORT..."
uv run python -m app > "$LOG" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null; wait $PID 2>/dev/null' EXIT

# Wait for server to be ready
for i in $(seq 1 30); do
    if curl -s "http://localhost:$PORT/health" > /dev/null 2>&1; then
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "Server failed to start. Logs:"
        cat "$LOG"
        exit 1
    fi
    sleep 1
done

echo "Server ready (PID $PID)"
echo ""

./tests/smoke_test.sh "http://localhost:$PORT"
