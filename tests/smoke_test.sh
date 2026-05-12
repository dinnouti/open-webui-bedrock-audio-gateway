#!/usr/bin/env bash
set -euo pipefail

# Smoke test suite for the audio gateway.
# Usage:
#   ./tests/smoke_test.sh              # test against localhost:8000
#   ./tests/smoke_test.sh http://host:port  # test against custom URL

BASE_URL="${1:-http://localhost:8000}"
API_KEY="${GATEWAY_API_KEY:-changeme}"
PASS=0
FAIL=0
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1 — $2"; }

echo "Running smoke tests against $BASE_URL"
echo "========================================"

# --- Health ---
echo ""
echo "[Health]"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health")
if [ "$STATUS" = "200" ]; then pass "GET /health → 200"; else fail "GET /health" "got $STATUS"; fi

# --- Discovery (no auth) ---
echo ""
echo "[Discovery]"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/v1/audio/models")
if [ "$STATUS" = "200" ]; then pass "GET /v1/audio/models → 200"; else fail "GET /v1/audio/models" "got $STATUS"; fi

VOICE_COUNT=$(curl -s "$BASE_URL/v1/audio/voices" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('voices',[])))")
if [ "$VOICE_COUNT" -gt 0 ]; then pass "GET /v1/audio/voices → $VOICE_COUNT voices"; else fail "GET /v1/audio/voices" "got 0 voices"; fi

# --- Auth ---
echo ""
echo "[Auth]"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/v1/audio/transcriptions" -F "file=@/dev/null")
if [ "$STATUS" = "401" ]; then pass "No token → 401"; else fail "No token" "got $STATUS"; fi

STATUS=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer wrong" -X POST "$BASE_URL/v1/audio/transcriptions" -F "file=@/dev/null")
if [ "$STATUS" = "401" ]; then pass "Bad token → 401"; else fail "Bad token" "got $STATUS"; fi

# --- Validation ---
echo ""
echo "[Validation]"
BODY=$(curl -s -H "Authorization: Bearer $API_KEY" -F "file=@/dev/null;filename=test.wav" "$BASE_URL/v1/audio/transcriptions")
if echo "$BODY" | grep -q "Empty audio file"; then pass "Empty file → rejected"; else fail "Empty file" "$BODY"; fi

BODY=$(curl -s -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" -d '{}' "$BASE_URL/v1/audio/speech")
if echo "$BODY" | grep -q "Missing 'input' field"; then pass "Missing input → rejected"; else fail "Missing input" "$BODY"; fi

LONG_PAYLOAD=$(python3 -c "import json; print(json.dumps({'input': 'x'*3001}))")
BODY=$(curl -s -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" -d "$LONG_PAYLOAD" "$BASE_URL/v1/audio/speech")
if echo "$BODY" | grep -q "Text too long"; then pass "Text too long → rejected"; else fail "Text too long" "$BODY"; fi

# --- TTS (requires AWS creds) ---
echo ""
echo "[TTS]"
STATUS=$(curl -s -o "$TMPDIR/tts.mp3" -w "%{http_code}" -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"input":"Hello, this is a smoke test.","voice":"Matthew"}' "$BASE_URL/v1/audio/speech")
if [ "$STATUS" = "200" ]; then
    TYPE=$(file -b "$TMPDIR/tts.mp3" | head -1)
    if echo "$TYPE" | grep -qi "audio\|mpeg\|ID3"; then pass "TTS → valid MP3"; else fail "TTS" "not audio: $TYPE"; fi
else
    fail "TTS" "HTTP $STATUS — $(cat "$TMPDIR/tts.mp3")"
fi

# --- STT (requires AWS creds + ffmpeg) ---
echo ""
echo "[STT]"
if [ -f "$TMPDIR/tts.mp3" ] && [ "$STATUS" = "200" ]; then
    BODY=$(curl -s -H "Authorization: Bearer $API_KEY" -F "file=@$TMPDIR/tts.mp3;filename=test.mp3" "$BASE_URL/v1/audio/transcriptions")
    if echo "$BODY" | python3 -c "import sys,json; t=json.load(sys.stdin).get('text',''); exit(0 if len(t)>5 else 1)" 2>/dev/null; then
        TEXT=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['text'])")
        pass "STT → \"$TEXT\""
    else
        fail "STT" "$BODY"
    fi
else
    echo "  SKIP: STT (no TTS audio to transcribe)"
fi

# --- Invalid voice fallback ---
echo ""
echo "[Voice fallback]"
STATUS=$(curl -s -o "$TMPDIR/fallback.mp3" -w "%{http_code}" -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"input":"Fallback test.","voice":"NonExistentVoice"}' "$BASE_URL/v1/audio/speech")
if [ "$STATUS" = "200" ]; then pass "Invalid voice → fallback to default"; else fail "Invalid voice fallback" "HTTP $STATUS"; fi

# --- Summary ---
echo ""
echo "========================================"
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then exit 1; fi
