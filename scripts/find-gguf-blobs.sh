#!/usr/bin/env bash
# find-gguf-blobs.sh — discover Ollama model blobs on the host and print
# llama-swap-compatible config snippets for each GGUF found.
#
# Usage:
#   ./scripts/find-gguf-blobs.sh
#
# Scans common Ollama blob directories and outputs YAML blocks that can be
# pasted into config/llama-swap.yaml.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS_DIR="${MODELS_DIR:-/opt/apps/ollama-models}"

# Common Ollama blob locations
OLLAMA_DIRS=(
    "$MODELS_DIR"
    "$HOME/.ollama/models"
    "/usr/share/ollama/.ollama/models"
    "/var/lib/ollama/models"
)

echo "Scanning for GGUF blobs..."
echo "Models dir: $MODELS_DIR"
echo

found=0

for dir in "${OLLAMA_DIRS[@]}"; do
    if [[ ! -d "$dir" ]]; then
        continue
    fi

    echo "Scanning: $dir"

    # Ollama uses content-addressed blobs: blobs/sha256-<hash>
    # GGUF files are stored as blobs with .gguf extension in their filename
    find "$dir" -type f -name "*.gguf" 2>/dev/null | while read -r gguf; do
        # Extract model name from path
        local_name=$(basename "$gguf" .gguf)

        # Try to get the alias from Ollama's manifest if available
        # Ollama stores manifests in models/manifests
        manifest_dir=$(dirname "$dir")/manifests/registry.ollama.ai
        if [[ -d "$manifest_dir" ]]; then
            # Try to find a matching manifest
            alias=$(find "$manifest_dir" -name "*.json" -exec grep -l "$local_name" {} \; 2>/dev/null | head -1 | xargs basename .json 2>/dev/null || true)
            if [[ -n "$alias" ]]; then
                local_name="$alias"
            fi
        fi

        echo "  GGUF: $gguf"
        echo "  name: $local_name"
        echo "  path: $gguf"
        echo

        found=$((found + 1))
    done
done

if [[ $found -eq 0 ]]; then
    echo "No GGUF blobs found."
    echo
    echo "To use models downloaded via Ollama, ensure MODELS_DIR points to"
    echo "the Ollama blob store (default: /opt/apps/ollama-models)."
    echo
    echo "If you downloaded GGUFs directly from Hugging Face, point"
    echo "MODELS_DIR at that directory and run 'make models-apply'."
fi
