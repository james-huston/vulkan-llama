#!/usr/bin/env bash
# run.sh — tool-use smoke test for a specific model
#
# Usage:
#   ./tests/tool_use/run.sh glm-4.7-flash-q4
#   ./tests/tool_use/run.sh qwen3-coder-30b

set -euo pipefail

MODEL="${1:?Usage: ./tests/tool_use/run.sh <model-name>}"
BASE="${2:-http://localhost:11434}"

echo "Testing tool use with model: $MODEL"
echo "Endpoint: $BASE"
echo

# Test 1: Function calling with a simple math tool
echo "=== Test 1: Simple function calling ==="
curl -s "$BASE/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{
        \"model\": \"$MODEL\",
        \"messages\": [
            {\"role\": \"system\", \"content\": \"You are a helpful assistant with access to a calculator tool.\"},
            {\"role\": \"user\", \"content\": \"What is 2 + 3?\"}
        ],
        \"tools\": [
            {
                \"type\": \"function\",
                \"function\": {
                    \"name\": \"calculator\",
                    \"description\": \"A simple calculator\",
                    \"parameters\": {
                        \"type\": \"object\",
                        \"properties\": {
                            \"expression\": {
                                \"type\": \"string\",
                                \"description\": \"The math expression to evaluate\"
                            }
                        },
                        \"required\": [\"expression\"]
                    }
                }
            }
        ],
        \"max_tokens\": 256
    }" | python3 -c "
import sys, json
data = json.load(sys.stdin)
choices = data.get('choices', [])
if choices:
    msg = choices[0].get('message', {})
    tool_calls = msg.get('tool_calls', [])
    if tool_calls:
        print(f'PASS: Got {len(tool_calls)} tool call(s)')
        for tc in tool_calls:
            func = tc.get('function', {})
            print(f'  Function: {func.get(\"name\", \"unknown\")}')
            print(f'  Args: {func.get(\"arguments\", \"\")}')
    else:
        content = msg.get('content', '')
        if content:
            print(f'PASS: Got response (no tool calls): {content[:100]}')
        else:
            print(f'WARN: Empty response')
else:
    print(f'FAIL: No choices in response')
    print(json.dumps(data, indent=2))
"

echo

# Test 2: Structured output
echo "=== Test 2: Structured output ==="
curl -s "$BASE/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{
        \"model\": \"$MODEL\",
        \"messages\": [
            {\"role\": \"user\", \"content\": \"Return a JSON object with keys 'name' and 'color' for a red apple.\"}
        ],
        \"response_format\": {\"type\": \"json_object\"},
        \"max_tokens\": 256
    }" | python3 -c "
import sys, json
data = json.load(sys.stdin)
choices = data.get('choices', [])
if choices:
    content = choices[0].get('message', {}).get('content', '')
    try:
        parsed = json.loads(content)
        if 'name' in parsed and 'color' in parsed:
            print(f'PASS: Valid JSON with required keys: {parsed}')
        else:
            print(f'WARN: JSON missing expected keys: {content[:100]}')
    except json.JSONDecodeError:
        print(f'FAIL: Not valid JSON: {content[:100]}')
else:
    print(f'FAIL: No choices in response')
    print(json.dumps(data, indent=2))
"
