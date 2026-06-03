#!/usr/bin/env bash
#
# add-model.sh
#
# Download a GGUF from Hugging Face into your model store and/or append a
# ready-to-run model block to config/llama-swap.yaml. Designed around the
# Battlemage (Arc Pro B70/B60/B50) defaults used throughout this repo:
#   -ngl 99  --device SYCL0  -sm none  --jinja
#
# Normally invoked via the Makefile, e.g.:
#
#   make add-model \
#       REPO=unsloth/GLM-4.7-Flash-GGUF \
#       FILE=GLM-4.7-Flash-Q4_K_XL.gguf \
#       NAME=glm-4.7-flash-q4 DIR=glm-4.7-flash OUT=Q4_K_XL.gguf \
#       CTX=131072 TEMPLATE=glm-4.7-flash.jinja REASONING=1 TEMP=0.6 TOP_P=0.95
#
# ...but it also runs standalone. See --help.
#
# Run this on the HOST (not inside the container). The container only ever
# reads /models/ — downloads land in the host MODELS_DIR.

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate the repo (this script lives in <repo>/scripts/).
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
DEFAULT_CONFIG="${REPO_ROOT}/config/llama-swap.yaml"
EXAMPLE_CONFIG="${REPO_ROOT}/config/llama-swap.example.yaml"
TEMPLATES_DIR="${REPO_ROOT}/templates"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
REPO=""           # HF repo id, e.g. unsloth/GLM-4.7-Flash-GGUF
FILE=""           # filename within the repo (supports a glob for split GGUFs)
NAME=""           # config alias (and default subdir)
DIR=""            # subdir under MODELS_DIR (default: NAME)
OUT=""            # local filename to save as (default: basename of FILE)
MODEL_PATH=""     # explicit container path; overrides DIR/OUT (e.g. a blob)
CTX="131072"      # -c context size
TEMPLATE=""       # jinja filename under templates/ -> --chat-template-file
REASONING=""      # non-empty -> add --reasoning-format deepseek
TEMP=""           # --temp
TOP_P=""          # --top-p
EXTRA=""          # extra raw llama-server flags, appended verbatim (one line)
TTL="600"         # llama-swap ttl
BRANCH="main"     # HF revision
CONFIG="${DEFAULT_CONFIG}"
DO_DOWNLOAD=1
DO_CONFIG=1
DRY_RUN=0

usage() {
    cat <<'EOF'
add-model.sh — download a GGUF from Hugging Face and/or add it to llama-swap.

Usage:
  scripts/add-model.sh --repo REPO --file FILE --name NAME [options]

Required (for download):
  --repo REPO        Hugging Face repo id (e.g. unsloth/GLM-4.7-Flash-GGUF)
  --file FILE        Filename in the repo. A glob (e.g. '*Q4_K_M*.gguf' or a
                     split set '*-00001-of-*') is allowed but needs the
                     huggingface CLI (hf / huggingface-cli) installed.
Required (always):
  --name NAME        Model alias used in the config and as the OpenAI model id.

Options:
  --dir DIR          Subdir under MODELS_DIR to store the GGUF (default: NAME).
  --out FILE         Save/reference the GGUF under this filename (default:
                     basename of --file). Single-file downloads only.
  --model-path PATH  Container path to put in --model verbatim, e.g.
                     /models/blobs/sha256-...  Overrides --dir/--out and skips
                     the dir-layout logic. Implies --no-download.
  --ctx N            Context size for -c (default: 131072). Cap dense 24B
                     Mistral models at 65536 — they segfault at 128k on SYCL.
  --template FILE    Jinja chat template in templates/ -> --chat-template-file.
  --reasoning        Add --reasoning-format deepseek (safe on non-thinkers).
  --temp X           Add --temp X.
  --top-p X          Add --top-p X.
  --extra "FLAGS"    Extra llama-server flags, appended verbatim.
  --ttl N            llama-swap ttl in seconds (default: 600).
  --branch REV       HF revision to download (default: main).
  --config PATH      Config file to edit (default: config/llama-swap.yaml).
  --no-download      Only edit the config; don't download.
  --no-config        Only download; don't touch the config.
  --dry-run          Print the config block instead of writing it.
  -h, --help         This help.

MODELS_DIR is resolved from: the MODELS_DIR env var, then an uncommented
MODELS_DIR= line in .env, then the repo default /opt/apps/ollama-models.
EOF
}

die() { echo "error: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)       REPO="$2"; shift 2 ;;
        --file)       FILE="$2"; shift 2 ;;
        --name)       NAME="$2"; shift 2 ;;
        --dir)        DIR="$2"; shift 2 ;;
        --out)        OUT="$2"; shift 2 ;;
        --model-path) MODEL_PATH="$2"; shift 2 ;;
        --ctx)        CTX="$2"; shift 2 ;;
        --template)   TEMPLATE="$2"; shift 2 ;;
        --reasoning)  REASONING=1; shift ;;
        --temp)       TEMP="$2"; shift 2 ;;
        --top-p)      TOP_P="$2"; shift 2 ;;
        --extra)      EXTRA="$2"; shift 2 ;;
        --ttl)        TTL="$2"; shift 2 ;;
        --branch)     BRANCH="$2"; shift 2 ;;
        --config)     CONFIG="$2"; shift 2 ;;
        --no-download) DO_DOWNLOAD=0; shift ;;
        --no-config)  DO_CONFIG=0; shift ;;
        --dry-run)    DRY_RUN=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

[[ -n "${MODEL_PATH}" ]] && DO_DOWNLOAD=0

# ---------------------------------------------------------------------------
# Resolve MODELS_DIR (env > .env > default), matching docker-compose's default.
# ---------------------------------------------------------------------------
resolve_models_dir() {
    if [[ -n "${MODELS_DIR:-}" ]]; then echo "${MODELS_DIR}"; return; fi
    if [[ -f "${ENV_FILE}" ]]; then
        local v
        v="$(grep -E '^[[:space:]]*MODELS_DIR[[:space:]]*=' "${ENV_FILE}" 2>/dev/null \
              | tail -n1 | cut -d= -f2- | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        if [[ -n "${v}" ]]; then echo "${v}"; return; fi
    fi
    echo "/opt/apps/ollama-models"
}
MODELS_DIR="$(resolve_models_dir)"

# ---------------------------------------------------------------------------
# Validate the combination of inputs.
# ---------------------------------------------------------------------------
[[ -n "${NAME}" ]] || die "--name is required."
DIR="${DIR:-${NAME}}"

if [[ "${DO_DOWNLOAD}" -eq 1 ]]; then
    [[ -n "${REPO}" ]] || die "--repo is required to download (or pass --no-download / --model-path)."
    # --file is required for Hugging Face; for Civitai it's optional (defaults
    # to the version's primary file).
    if [[ "${REPO}" != civitai:* ]]; then
        [[ -n "${FILE}" ]] || die "--file is required to download (or pass --no-download / --model-path)."
    fi
fi

if [[ -n "${TEMPLATE}" && ! -f "${TEMPLATES_DIR}/${TEMPLATE}" ]]; then
    echo "warning: templates/${TEMPLATE} not found in this repo." >&2
    echo "         The container mounts ./templates at /templates — add it there first." >&2
fi

is_glob() { [[ "$1" == *"*"* || "$1" == *"?"* || "$1" == *"["* ]]; }

# ---------------------------------------------------------------------------
# Civitai support
#
# REPO=civitai:<modelVersionId> downloads the primary file of that version from
# Civitai's API. CIVITAI_API_KEY (env > .env) is sent as Bearer auth — required
# for gated files, recommended even for public ones (better rate limits). The
# SHA256 is verified when Civitai surfaces it. --file optionally selects a
# non-primary file in a multi-file version.
# ---------------------------------------------------------------------------
civitai_resolve_key() {
    if [[ -n "${CIVITAI_API_KEY:-}" ]]; then echo "${CIVITAI_API_KEY}"; return; fi
    if [[ -f "${ENV_FILE}" ]]; then
        local v
        v="$(grep -E '^[[:space:]]*CIVITAI_API_KEY[[:space:]]*=' "${ENV_FILE}" 2>/dev/null \
              | tail -n1 | cut -d= -f2- | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/^"//; s/"$//')"
        [[ -n "${v}" ]] && { echo "${v}"; return; }
    fi
    echo ""
}

civitai_download() {
    local version_id="$1"
    [[ -n "${version_id}" ]] || die "civitai: missing modelVersionId (use REPO=civitai:<id>)."

    local key=""
    local auth=()
    key="$(civitai_resolve_key)"
    if [[ -n "${key}" ]]; then auth=(-H "Authorization: Bearer ${key}"); fi

    echo "==> Downloading from Civitai"
    echo "    version:  ${version_id}"
    echo "    auth:     $([ -n "${key}" ] && echo 'CIVITAI_API_KEY (Bearer)' || echo 'unauthenticated (best-effort)')"
    echo "    into:     ${DEST_DIR}/"

    if [[ ! -d "${MODELS_DIR}" ]]; then
        die "MODELS_DIR does not exist: ${MODELS_DIR}
       Create it, or set MODELS_DIR (env or .env) to a path you own."
    fi
    if ! mkdir -p "${DEST_DIR}" 2>/dev/null || [[ ! -w "${DEST_DIR}" ]]; then
        die "cannot write to ${DEST_DIR} — pick a writable MODELS_DIR."
    fi

    # 1. Fetch version metadata
    local meta_status meta_file=/tmp/civi_meta.$$.json
    meta_status="$(curl -sS --max-time 25 "${auth[@]}" -o "${meta_file}" -w '%{http_code}' \
        "https://civitai.com/api/v1/model-versions/${version_id}")"
    case "${meta_status}" in
        200) ;;
        401|403) rm -f "${meta_file}"; die "civitai metadata ${meta_status}: auth/gating on version ${version_id}.
       Set CIVITAI_API_KEY in .env, or accept this model's terms on the Civitai web UI." ;;
        404)     rm -f "${meta_file}"; die "civitai metadata 404: no such modelVersionId ${version_id}.
       Open the model on civitai.com, pick a version, and use the modelVersionId from the URL." ;;
        *)       rm -f "${meta_file}"; die "civitai metadata HTTP ${meta_status}." ;;
    esac

    # 2. Pick a file: --file by name if given, else primary, else first .safetensors
    local picked
    picked="$(WANT_FILE="${FILE:-}" python3 -c "
import json, os, sys
data = json.load(sys.stdin)
want = os.environ.get('WANT_FILE', '')
files = data.get('files') or []
chosen = None
if want:
    chosen = next((f for f in files if f.get('name') == want), None)
if not chosen:
    chosen = next((f for f in files if f.get('primary')), None)
if not chosen:
    chosen = next((f for f in files if (f.get('name') or '').endswith('.safetensors')), None)
if not chosen:
    print('__NONE__'); sys.exit()
print(chosen.get('name', ''))
print(chosen.get('downloadUrl', ''))
print((chosen.get('hashes') or {}).get('SHA256', ''))
print(chosen.get('sizeKB') or '')
" < "${meta_file}")"
    rm -f "${meta_file}"
    [[ "${picked%%$'\n'*}" == "__NONE__" ]] && die "civitai: no usable file in version ${version_id}. Pass --file <name>."

    local cv_name cv_url cv_sha cv_size
    cv_name="$(echo "$picked" | sed -n 1p)"
    cv_url="$( echo "$picked" | sed -n 2p)"
    cv_sha="$( echo "$picked" | sed -n 3p)"
    cv_size="$(echo "$picked" | sed -n 4p)"

    local target="${DEST_DIR}/${OUT:-${cv_name}}"
    echo "    file:     ${cv_name} (${cv_size%.*} KB)"
    [[ -n "$cv_sha" ]] && echo "    sha256:   ${cv_sha:0:16}…"

    # 3. Download (curl follows 307 -> R2 signed URL via -L; -C - resumes)
    local dl_status
    dl_status="$(curl -SL --retry 3 -C - "${auth[@]}" -o "${target}" \
        -w '%{http_code}' "${cv_url}")"
    case "${dl_status}" in
        200|206) ;;
        401|403) die "civitai download ${dl_status}: auth/gating on the file.
       Set CIVITAI_API_KEY in .env, or accept this model's terms on the Civitai web UI." ;;
        404)     die "civitai download 404." ;;
        *)       die "civitai download HTTP ${dl_status}." ;;
    esac

    # 4. SHA256 verify if surfaced
    if [[ -n "${cv_sha}" ]] && command -v sha256sum >/dev/null 2>&1; then
        local got
        got="$(sha256sum "${target}" | awk '{print toupper($1)}')"
        if [[ "${got}" == "${cv_sha}" ]]; then
            echo "    sha256:   OK"
        else
            echo "    sha256:   MISMATCH — expected ${cv_sha:0:16}…, got ${got:0:16}…" >&2
            die "civitai: SHA256 mismatch; delete the file and retry."
        fi
    fi
}

# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
DEST_DIR="${MODELS_DIR%/}/${DIR}"

download() {
    if [[ "${REPO}" == civitai:* ]]; then
        civitai_download "${REPO#civitai:}"
        return
    fi

    echo "==> Downloading from Hugging Face"
    echo "    repo:   ${REPO} (@${BRANCH})"
    echo "    file:   ${FILE}"
    echo "    into:   ${DEST_DIR}/"

    if [[ ! -d "${MODELS_DIR}" ]]; then
        die "MODELS_DIR does not exist: ${MODELS_DIR}
       Create it, or set MODELS_DIR (env or .env) to a path you own."
    fi
    if ! mkdir -p "${DEST_DIR}" 2>/dev/null || [[ ! -w "${DEST_DIR}" ]]; then
        die "cannot write to ${DEST_DIR}
       MODELS_DIR (${MODELS_DIR}) isn't writable by you. The Ollama blob dir is
       usually root-owned — set MODELS_DIR in .env to a directory you own and
       point docker-compose's volume mount at it."
    fi

    local hf_cli=""
    if command -v hf >/dev/null 2>&1; then
        hf_cli="hf"
    elif command -v huggingface-cli >/dev/null 2>&1; then
        hf_cli="huggingface-cli"
    fi

    if is_glob "${FILE}"; then
        [[ -n "${hf_cli}" ]] || die "downloading a glob/split set needs the huggingface CLI.
       Install it with:  pip install -U \"huggingface_hub[cli]\"
       (or pass a single concrete --file and download with curl)."
        echo "    using:  ${hf_cli} (glob pattern)"
        "${hf_cli}" download "${REPO}" --include "${FILE}" \
            --revision "${BRANCH}" --local-dir "${DEST_DIR}"
        return
    fi

    if [[ -n "${hf_cli}" ]]; then
        echo "    using:  ${hf_cli}"
        "${hf_cli}" download "${REPO}" "${FILE}" \
            --revision "${BRANCH}" --local-dir "${DEST_DIR}"
    else
        local url="https://huggingface.co/${REPO}/resolve/${BRANCH}/${FILE}?download=true"
        local target="${DEST_DIR}/$(basename "${FILE}")"
        echo "    using:  curl/wget (no huggingface CLI found)"
        if command -v curl >/dev/null 2>&1; then
            curl -fL --retry 3 -C - -o "${target}" "${url}"
        elif command -v wget >/dev/null 2>&1; then
            wget -c -O "${target}" "${url}"
        else
            die "need one of: hf, huggingface-cli, curl, wget."
        fi
    fi

    # Optional rename to --out for a single concrete file.
    if [[ -n "${OUT}" ]]; then
        local src="${DEST_DIR}/$(basename "${FILE}")"
        if [[ -f "${src}" && "$(basename "${FILE}")" != "${OUT}" ]]; then
            echo "    rename: $(basename "${FILE}") -> ${OUT}"
            mv -f "${src}" "${DEST_DIR}/${OUT}"
        fi
    fi
}

# ---------------------------------------------------------------------------
# Work out the on-disk filename to reference in the config.
# ---------------------------------------------------------------------------
resolve_model_filename() {
    if [[ -n "${OUT}" ]]; then echo "${OUT}"; return; fi
    if [[ -n "${FILE}" ]] && ! is_glob "${FILE}"; then echo "$(basename "${FILE}")"; return; fi
    # Glob (split set) or config-only with no --out: pick the first shard, else
    # the lone .gguf in the dir.
    if [[ -d "${DEST_DIR}" ]]; then
        local first
        first="$(find "${DEST_DIR}" -maxdepth 1 -name '*-00001-of-*.gguf' -printf '%f\n' 2>/dev/null | sort | head -n1)"
        [[ -n "${first}" ]] && { echo "${first}"; return; }
        local only
        mapfile -t only < <(find "${DEST_DIR}" -maxdepth 1 -name '*.gguf' -printf '%f\n' 2>/dev/null)
        [[ "${#only[@]}" -eq 1 ]] && { echo "${only[0]}"; return; }
    fi
    return 1
}

# ---------------------------------------------------------------------------
# Build the config block (matches the indentation used across the file).
# ---------------------------------------------------------------------------
generate_block() {
    local model_path="$1"
    printf '\n'
    printf '  # %s — added by scripts/add-model.sh on %s\n' "${NAME}" "$(date +%Y-%m-%d)"
    printf '  %s:\n' "${NAME}"
    printf '    cmd: |\n'
    printf '      /opt/llama-cpp/bin/llama-server\n'
    printf '      --port ${PORT}\n'
    printf '      --model %s\n' "${model_path}"
    printf '      --alias %s\n' "${NAME}"
    printf '      --host 127.0.0.1\n'
    printf '      -ngl 99\n'
    printf '      -c %s\n' "${CTX}"
    printf '      --device SYCL0\n'
    printf '      -sm none\n'
    printf '      --jinja\n'
    [[ -n "${TEMPLATE}"  ]] && printf '      --chat-template-file /templates/%s\n' "${TEMPLATE}"
    [[ -n "${REASONING}" ]] && printf '      --reasoning-format deepseek\n'
    [[ -n "${TEMP}"      ]] && printf '      --temp %s\n' "${TEMP}"
    [[ -n "${TOP_P}"     ]] && printf '      --top-p %s\n' "${TOP_P}"
    [[ -n "${EXTRA}"     ]] && printf '      %s\n' "${EXTRA}"
    printf '    ttl: %s\n' "${TTL}"
}

add_config() {
    if [[ ! -f "${CONFIG}" && "${DRY_RUN}" -eq 0 ]]; then
        die "config not found: ${CONFIG}
       Create it first:  cp ${EXAMPLE_CONFIG} ${CONFIG}"
    fi

    local model_path
    if [[ -n "${MODEL_PATH}" ]]; then
        model_path="${MODEL_PATH}"
    else
        local fname
        if ! fname="$(resolve_model_filename)"; then
            die "couldn't determine the GGUF filename for the config.
       Pass --out <file> (single file) or --model-path <container path>."
        fi
        model_path="/models/${DIR}/${fname}"
    fi

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "==> Config block (dry run — not written):"
        generate_block "${model_path}"
        return
    fi

    if grep -qE "^[[:space:]]{2}${NAME}:[[:space:]]*$" "${CONFIG}"; then
        die "a model named '${NAME}' already exists in ${CONFIG}.
       Pick a different --name, or edit/remove the existing block."
    fi

    echo "==> Adding '${NAME}' to ${CONFIG}"
    echo "    --model ${model_path}"
    generate_block "${model_path}" >> "${CONFIG}"
}

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
echo "MODELS_DIR = ${MODELS_DIR}"
[[ "${DO_DOWNLOAD}" -eq 1 ]] && download
[[ "${DO_CONFIG}"   -eq 1 ]] && add_config

if [[ "${DRY_RUN}" -eq 0 && "${DO_CONFIG}" -eq 1 ]]; then
    cat <<EOF

Done. Next steps:
  * llama-swap hot-reloads config/llama-swap.yaml — the next request to
    '${NAME}' will spawn it (first request is a 10-30s cold start).
  * Smoke test:
      curl http://localhost:11434/v1/chat/completions \\
        -H "Content-Type: application/json" \\
        -d '{"model":"${NAME}","messages":[{"role":"user","content":"hi"}]}'
  * Validate tool calling:
      ./tests/tool_use/run.sh ${NAME}
EOF
fi
