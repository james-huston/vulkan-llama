#!/usr/bin/env bash
# run.sh — basic smoke test for a model
#
# Usage:
#   ./tests/smoke/run.sh qwen3-14b
#   ./tests/smoke/run.sh glm-4.7-flash-q4

set -euo pipefail

MODEL="${1:?Usage: ./tests/smoke/run.sh <model-name>}"
BASE="${2:-http://localhost:11434}"

echo "Smoke testing model: $MODEL"
echo "Endpoint: $BASE"
echo

# Send a simple chat request
echo "=== Chat completion ==="
RESPONSE=$(curl -s "$BASE/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{
        \"model\": \"$MODEL\",
        \"messages\": [
            {\"role\": \"user\", \"content\": \"Say hello in exactly 3 words.\"}
        ],
        \"max_tokens\": 50,
        \"temperature\": 0.7
    }")

# Pretty print the response
echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
choices = data.get('choices', [])
if choices:
    msg = choices[0].get('message', {})
    content = msg.get('content', '')
    finish = choices[0].get('finish_reason', '')
    print(f'Response: {content}')
    print(f'Finish reason: {finish}')
    if content.strip():
        print('PASS: Got a response')
    else:
        print('WARN: Empty response content')
else:
    print('FAIL: No choices in response')
    print(json.dumps(data, indent=2))
"

echo

# Check that the model was loaded
echo "=== Model info ==="
curl -s "$BASE/v1/models" | python3 -c "
import sys, json
data = json.load(sys.stdin)
models = [m['id'] for m in data.get('data', [])]
print(f'Available models: {len(models)}')
for m in sorted(models):
    print(f'  - {m}')
"
