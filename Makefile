# battlemage-llama — convenience targets for managing models and the stack.
#
# Model management (see `make help`):
#   make add-model REPO=<hf-repo> FILE=<gguf> NAME=<alias> [opts]
#       Download a GGUF from Hugging Face into MODELS_DIR and append a
#       ready-to-run block to config/llama-swap.yaml.
#
# All model vars are passed through to scripts/add-model.sh; run it with
# --help for the full list. The common ones:
#   REPO   HF repo id            FILE   filename in the repo (glob ok)
#   NAME   config alias          DIR    subdir under MODELS_DIR (default NAME)
#   OUT    save-as filename      CTX    context size (default 131072)
#   TEMPLATE  jinja in templates/  REASONING=1  add --reasoning-format deepseek
#   TEMP / TOP_P  samplers       EXTRA  extra llama-server flags (quoted)
#   BRANCH  HF revision          MODELS_DIR  override the model store
#   DRY_RUN=1  print the config block instead of writing it

SHELL    := /usr/bin/env bash
SCRIPTS  := ./scripts
CONFIG   ?= config/llama-swap.yaml
ADD      := $(SCRIPTS)/add-model.sh

# Translate Make variables into add-model.sh flags. Values here never contain
# spaces (HF ids, filenames, numbers); EXTRA may, so it's quoted.
MODEL_ARGS := \
	$(if $(REPO),--repo $(REPO)) \
	$(if $(FILE),--file $(FILE)) \
	$(if $(NAME),--name $(NAME)) \
	$(if $(DIR),--dir $(DIR)) \
	$(if $(OUT),--out $(OUT)) \
	$(if $(MODEL_PATH),--model-path $(MODEL_PATH)) \
	$(if $(CTX),--ctx $(CTX)) \
	$(if $(TEMPLATE),--template $(TEMPLATE)) \
	$(if $(REASONING),--reasoning) \
	$(if $(TEMP),--temp $(TEMP)) \
	$(if $(TOP_P),--top-p $(TOP_P)) \
	$(if $(TTL),--ttl $(TTL)) \
	$(if $(BRANCH),--branch $(BRANCH)) \
	$(if $(CONFIG),--config $(CONFIG)) \
	$(if $(EXTRA),--extra "$(EXTRA)") \
	$(if $(DRY_RUN),--dry-run)

SYNC := $(SCRIPTS)/sync-litellm.py
TEST := $(SCRIPTS)/test-models.py
APPLY := $(SCRIPTS)/apply-models.py
STATUS := $(SCRIPTS)/status.py

.DEFAULT_GOAL := help
.PHONY: help add-model download-model add-config list-models find-blobs sync-litellm test-models models-apply status

help: ## Show this help
	@echo "battlemage-llama — make targets:"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Example:"
	@echo "  make add-model REPO=unsloth/Ministral-3-14B-Reasoning-2512-GGUF \\"
	@echo "      FILE='*Q4_K_M*.gguf' NAME=ministral-3-14b-q4 DIR=ministral-3-14b-reasoning \\"
	@echo "      OUT=Q4_K_M.gguf CTX=131072 TEMPLATE=ministral-3-reasoning.jinja \\"
	@echo "      REASONING=1 TEMP=0.7 TOP_P=0.95"

add-model: ## Download a GGUF from HuggingFace AND add it to the config
	@$(ADD) $(MODEL_ARGS)

download-model: ## Download a GGUF only (no config change)
	@$(ADD) --no-config $(MODEL_ARGS)

add-config: ## Add a config block only (no download; needs OUT or MODEL_PATH)
	@$(ADD) --no-download $(MODEL_ARGS)

status: ## Show the currently loaded model + its CPU/RAM and GPU power/VRAM
	@$(STATUS)

list-models: ## List the model aliases currently in the config
	@test -f $(CONFIG) || { echo "no config at $(CONFIG) (cp config/llama-swap.example.yaml $(CONFIG))"; exit 1; }
	@echo "Models in $(CONFIG):"
	@grep -oE '^  [A-Za-z0-9._-]+:' $(CONFIG) | sed 's/[: ]//g' | sed 's/^/  /'

find-blobs: ## Map local Ollama models to container blob paths
	@$(SCRIPTS)/find-gguf-blobs.sh $(MODELS_DIR)

models-apply: ## Download enabled models from models.yaml and regenerate config/llama-swap.yaml
	@$(APPLY) \
		$(if $(MANIFEST),--manifest $(MANIFEST)) \
		$(if $(CONFIG),--config $(CONFIG)) \
		$(if $(DRY_RUN),--dry-run) \
		$(if $(NO_DOWNLOAD),--no-download)

sync-litellm: ## Mirror llama-swap's models into a LiteLLM proxy (adds new, deletes stale)
	@$(SYNC) \
		$(if $(UPSTREAM),--upstream $(UPSTREAM)) \
		$(if $(LITELLM),--litellm $(LITELLM)) \
		$(if $(API_BASE),--api-base $(API_BASE)) \
		$(if $(MODEL_API_KEY),--model-api-key $(MODEL_API_KEY)) \
		$(if $(PROVIDER),--provider $(PROVIDER)) \
		$(if $(DRY_RUN),--dry-run) \
		$(if $(NO_DELETE),--no-delete) \
		$(if $(RESET),--reset)

test-models: ## Load each served model and run a smoke query (VIA=upstream|litellm, MODELS=subset)
	@$(TEST) \
		$(if $(VIA),--via $(VIA)) \
		$(if $(BASE),--base $(BASE)) \
		$(if $(API_KEY),--api-key $(API_KEY)) \
		$(if $(MAX_TOKENS),--max-tokens $(MAX_TOKENS)) \
		$(if $(TIMEOUT),--timeout $(TIMEOUT)) \
		$(if $(PROMPT),--prompt "$(PROMPT)") \
		$(MODELS)
