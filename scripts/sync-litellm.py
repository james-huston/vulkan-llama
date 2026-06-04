#!/usr/bin/env python3
"""
sync-litellm.py

Make a LiteLLM proxy's model list mirror the models served by this stack's
llama-swap endpoint. Run on demand (e.g. after `make add-model`):

    LITELLM_API_KEY=sk-... ./scripts/sync-litellm.py

What it does, in one pass:
  * Reads the source model ids from   GET {upstream}/v1/models   (llama-swap).
  * Reads LiteLLM's current models via GET {litellm}/model/info.
  * Adds any source model missing from LiteLLM   (POST /model/new).
  * Deletes any LiteLLM-managed model not in the source (POST /model/delete).
  * Re-points an existing model if its api_base / underlying model drifted.

Only models LiteLLM added to its own DB (model_info.db_model == true) are ever
deleted or rewritten — models baked into LiteLLM's static config.yaml are left
alone (the API can't manage them anyway) and reported as skipped.

Configuration (CLI flag > real env var > .env in repo root > default):
  --upstream       UPSTREAM_URL    http://localhost:11434
                   Where THIS script reads /v1/models.
  --litellm        LITELLM_URL     http://localhost:4000
                   LiteLLM proxy admin base URL.
  --api-base       MODEL_API_BASE  {upstream}/v1
                   The api_base baked into each LiteLLM model. For a LiteLLM
                   server on another host this MUST be a routable URL, e.g.
                   http://10.0.0.5:11434/v1 — NOT localhost.
  --model-api-key  MODEL_API_KEY   dummy
                   api_key stored with each model (llama-swap ignores it, but
                   LiteLLM's OpenAI handler wants a non-empty value).
  --provider       MODEL_PROVIDER  openai
                   LiteLLM provider prefix (llama-swap is OpenAI-compatible).

The LiteLLM admin key is read from LITELLM_API_KEY or LITELLM_MASTER_KEY
(env or .env). It is never printed.

Flags: --dry-run (plan only), --no-delete (never remove), --timeout SECS.
"""

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(REPO_ROOT, ".env")


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def load_dotenv(path):
    """Minimal KEY=VALUE reader for the repo .env (ignores comments/blank/quotes)."""
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip().strip('"').strip("'")
            values[key.strip()] = val
    return values


DOTENV = load_dotenv(ENV_FILE)


def setting(cli_value, env_name, default):
    """Precedence: CLI flag > process env > repo .env > default."""
    if cli_value is not None:
        return cli_value
    if os.environ.get(env_name):
        return os.environ[env_name]
    if DOTENV.get(env_name):
        return DOTENV[env_name]
    return default


def api(method, url, key, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        return exc.code, detail
    except urllib.error.URLError as exc:
        die(f"cannot reach {url}: {exc.reason}")
    except TimeoutError:
        die(f"timed out reaching {url}")


def load_model_infos():
    """Map model name -> LiteLLM model_info dict, built from models.yaml.

    Each entry's `litellm:` block (description, supports_function_calling,
    supports_tool_choice, supports_reasoning, ...) is passed through. We also
    derive: mode=chat and supports_vision=false (fleet defaults; overridable in
    the block), max_input_tokens from the entry's `ctx`, and per-token costs from
    the entry's `decode_tps` + the top-level `cost:` knobs (electricity ×
    amortization). Output price ≈ watts/1000 × (1/decode_tps/3600) × $/kWh ×
    amortization; input = output / input_factor (prefill is faster than decode).

    Optional: if models.yaml or PyYAML is absent, this is simply skipped.
    """
    path = os.path.join(REPO_ROOT, "models.yaml")
    if not os.path.isfile(path):
        return {}
    try:
        import yaml
    except ImportError:
        print("note: PyYAML not installed — skipping model_info metadata.", file=sys.stderr)
        return {}
    with open(path, encoding="utf-8") as fh:
        manifest = yaml.safe_load(fh) or {}

    cost = manifest.get("cost") or {}
    watts = cost.get("watts")
    rate = cost.get("usd_per_kwh")
    amort = cost.get("amortization", 1)
    input_factor = cost.get("input_factor", 1)

    out = {}
    for m in manifest.get("models") or []:
        name = m.get("name")
        if not name:
            continue
        info = {}
        block = m.get("litellm")
        if isinstance(block, dict):
            info.update({k: (" ".join(v.split()) if isinstance(v, str) else v)
                         for k, v in block.items()})
        info.setdefault("mode", "chat")
        info.setdefault("supports_vision", False)
        if m.get("ctx"):
            info.setdefault("max_input_tokens", int(m["ctx"]))
        tps = m.get("decode_tps")
        if tps and watts and rate:
            out_cost = (watts / 1000.0) * (1.0 / float(tps) / 3600.0) * float(rate) * float(amort)
            info["output_cost_per_token"] = round(out_cost, 12)
            info["input_cost_per_token"] = round(out_cost / float(input_factor), 12)
        if info:
            out[name] = info
    return out


def fetch_source_models(upstream, timeout):
    url = f"{upstream.rstrip('/')}/v1/models"
    status, payload = api("GET", url, key=None, timeout=timeout)
    if status != 200 or not isinstance(payload, dict):
        die(f"GET {url} returned {status}: {payload}")
    ids = sorted({m["id"] for m in payload.get("data", []) if m.get("id")})
    if not ids:
        die(f"no models reported by {url} — is llama-swap running with a config?")
    return ids


def fetch_litellm_models(litellm, key, timeout):
    url = f"{litellm.rstrip('/')}/model/info"
    status, payload = api("GET", url, key=key, timeout=timeout)
    if status == 401:
        die("LiteLLM rejected the admin key (401). Check LITELLM_API_KEY / LITELLM_MASTER_KEY.")
    if status != 200 or not isinstance(payload, dict):
        die(f"GET {url} returned {status}: {payload}")
    models = {}
    for item in payload.get("data", []):
        name = item.get("model_name")
        if not name:
            continue
        info = item.get("model_info") or {}
        params = item.get("litellm_params") or {}
        models[name] = {
            "id": info.get("id"),
            "db_model": bool(info.get("db_model")),
            "model": params.get("model"),
            "api_base": params.get("api_base"),
            "info": info,
        }
    return models


def add_model(litellm, key, name, params, timeout, model_info=None):
    url = f"{litellm.rstrip('/')}/model/new"
    body = {"model_name": name, "litellm_params": params}
    if model_info:
        body["model_info"] = model_info
    status, payload = api("POST", url, key=key, body=body, timeout=timeout)
    if status not in (200, 201):
        die(f"failed to add '{name}' ({status}): {payload}")


def delete_model(litellm, key, model_id, timeout):
    url = f"{litellm.rstrip('/')}/model/delete"
    status, payload = api("POST", url, key=key, body={"id": model_id}, timeout=timeout)
    if status not in (200, 201):
        die(f"failed to delete id={model_id} ({status}): {payload}")


def parse_args():
    p = argparse.ArgumentParser(
        description="Sync llama-swap models into a LiteLLM proxy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--upstream", help="llama-swap base URL (default http://localhost:11434)")
    p.add_argument("--litellm", help="LiteLLM proxy base URL (default http://localhost:4000)")
    p.add_argument("--api-base", dest="api_base",
                   help="api_base stored in LiteLLM for each model (default {upstream}/v1)")
    p.add_argument("--model-api-key", dest="model_api_key",
                   help="api_key stored with each model (default 'dummy')")
    p.add_argument("--provider", help="LiteLLM provider prefix (default 'openai')")
    p.add_argument("--timeout", type=float, default=15.0, help="per-request timeout seconds")
    p.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    p.add_argument("--no-delete", action="store_true", help="never delete, only add/update")
    p.add_argument("--reset", action="store_true",
                   help="delete ALL DB-managed models first, then re-add from source")
    return p.parse_args()


def main():
    args = parse_args()

    upstream = setting(args.upstream, "UPSTREAM_URL", "http://localhost:11434")
    litellm = setting(args.litellm, "LITELLM_URL", "http://localhost:4000")
    api_base = setting(args.api_base, "MODEL_API_BASE", f"{upstream.rstrip('/')}/v1")
    model_api_key = setting(args.model_api_key, "MODEL_API_KEY", "dummy")
    provider = setting(args.provider, "MODEL_PROVIDER", "openai")

    admin_key = (os.environ.get("LITELLM_API_KEY") or os.environ.get("LITELLM_MASTER_KEY")
                 or DOTENV.get("LITELLM_API_KEY") or DOTENV.get("LITELLM_MASTER_KEY"))
    if not admin_key:
        die("no LiteLLM admin key. Set LITELLM_API_KEY (or LITELLM_MASTER_KEY) "
            "in the environment or in .env.")

    print(f"source  : {upstream.rstrip('/')}/v1/models")
    print(f"litellm : {litellm.rstrip('/')}/model/*")
    print(f"api_base: {api_base}  (provider: {provider})")
    if args.dry_run:
        print("mode    : DRY RUN — no changes will be made")
    print()
    sys.stdout.flush()

    source_ids = fetch_source_models(upstream, args.timeout)
    existing = fetch_litellm_models(litellm, admin_key, args.timeout)
    model_infos = load_model_infos()

    if args.reset:
        purge = [(name, info["id"]) for name, info in existing.items() if info["db_model"]]
        non_db = [name for name, info in existing.items() if not info["db_model"]]
        verb = "would delete" if args.dry_run else "deleting"
        print(f"reset: {verb} {len(purge)} DB-managed model(s) before re-adding")
        for name, model_id in purge:
            print(f"  - {name}")
            if not args.dry_run:
                delete_model(litellm, admin_key, model_id, args.timeout)
        for name in non_db:
            print(f"  skip {name} (config-defined, not deletable via API)")
        # Treat purged models as gone so the plan below re-adds everything fresh.
        existing = {name: info for name, info in existing.items() if not info["db_model"]}
        print()

    def desired_params(model_id):
        return {"model": f"{provider}/{model_id}", "api_base": api_base, "api_key": model_api_key}

    def desired_info(model_id):
        return model_infos.get(model_id) or None

    def info_drifted(model_id, cur):
        """True if any manifest model_info field differs from what LiteLLM has."""
        want = model_infos.get(model_id) or {}
        have = cur.get("info") or {}
        for k, v in want.items():
            hv = have.get(k)
            if isinstance(v, float) and isinstance(hv, (int, float)):
                if not math.isclose(float(hv), v, rel_tol=1e-6, abs_tol=1e-15):
                    return True
            elif hv != v:
                return True
        return False

    to_add, to_update, to_delete, skipped = [], [], [], []

    for model_id in source_ids:
        cur = existing.get(model_id)
        if cur is None:
            to_add.append(model_id)
        elif (cur["model"] != f"{provider}/{model_id}" or cur["api_base"] != api_base
              or info_drifted(model_id, cur)):
            if cur["db_model"]:
                to_update.append(model_id)
            else:
                skipped.append((model_id, "config-defined, can't rewrite"))

    for name, info in existing.items():
        if name in source_ids:
            continue
        if not info["db_model"]:
            skipped.append((name, "config-defined, not deletable via API"))
        elif args.no_delete:
            skipped.append((name, "stale, but --no-delete set"))
        else:
            to_delete.append((name, info["id"]))

    print(f"source models : {len(source_ids)}")
    print(f"  to add      : {len(to_add)}  {to_add if to_add else ''}")
    print(f"  to re-point : {len(to_update)}  {to_update if to_update else ''}")
    print(f"  to delete   : {len(to_delete)}  {[n for n, _ in to_delete] if to_delete else ''}")
    if skipped:
        for name, why in skipped:
            print(f"  skip        : {name} ({why})")
    print()

    if args.dry_run:
        print("dry run complete — nothing changed.")
        return

    for model_id in to_add:
        print(f"+ add    {model_id}")
        add_model(litellm, admin_key, model_id, desired_params(model_id), args.timeout,
                  model_info=desired_info(model_id))

    for model_id in to_update:
        print(f"~ repoint {model_id}")
        delete_model(litellm, admin_key, existing[model_id]["id"], args.timeout)
        add_model(litellm, admin_key, model_id, desired_params(model_id), args.timeout,
                  model_info=desired_info(model_id))

    for name, model_id in to_delete:
        print(f"- delete {name}")
        delete_model(litellm, admin_key, model_id, args.timeout)

    print()
    print(f"done: +{len(to_add)} added, ~{len(to_update)} re-pointed, "
          f"-{len(to_delete)} deleted.")


if __name__ == "__main__":
    main()
