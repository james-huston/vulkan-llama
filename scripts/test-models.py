#!/usr/bin/env python3
"""
test-models.py

Cycle through every model an OpenAI-compatible endpoint advertises, send a real
chat query to each (which forces llama-swap to load it), and report pass/fail.
A sequential smoke test — only one model fits in VRAM at a time, so each request
is effectively a cold start as llama-swap swaps models in and out.

    ./scripts/test-models.py                 # test llama-swap directly
    ./scripts/test-models.py --via litellm   # test through the LiteLLM proxy
    ./scripts/test-models.py qwen3-coder-30b glm-4.7-flash-q4   # a subset

The model list is fetched from {base}/v1/models, so each target is tested
against its own advertised models. Point it at LiteLLM to see exactly which
registered entries are broken (e.g. ones still using the ollama provider).

Configuration (CLI flag > real env var > .env in repo root > default):
  --via {upstream,litellm}        which endpoint to test (default upstream)
  --base URL        explicit base URL, overrides --via
  --api-key KEY     explicit api key, overrides --via
  --max-tokens N    response cap (default 256)
  --timeout SECS    per-request timeout, covers cold start (default 120)
  --prompt TEXT     the user prompt (default a trivial "reply OK")

  UPSTREAM_URL  (default http://localhost:11434)  llama-swap base
  LITELLM_URL   (default http://localhost:4000)   LiteLLM proxy base
  LITELLM_API_KEY / LITELLM_MASTER_KEY            key used when --via litellm

Exit code is non-zero if any model FAILS (a WARN — loaded but produced no
visible text, usually a thinking model hitting the token cap — does not fail).
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(REPO_ROOT, ".env")

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def load_dotenv(path):
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


DOTENV = load_dotenv(ENV_FILE)


def setting(cli_value, env_name, default):
    if cli_value is not None:
        return cli_value
    if os.environ.get(env_name):
        return os.environ[env_name]
    if DOTENV.get(env_name):
        return DOTENV[env_name]
    return default


def openai_base(url):
    """Normalize a base URL so we can append /v1/... regardless of trailing /v1."""
    u = url.rstrip("/")
    if u.endswith("/v1"):
        u = u[:-3].rstrip("/")
    return u


def image_models():
    """Names of non-chat models (engine != llama-server, or mode image_generation)
    from models.yaml, so the chat smoke test can skip them. Empty if PyYAML or the
    manifest is absent."""
    path = os.path.join(REPO_ROOT, "models.yaml")
    if not os.path.isfile(path):
        return set()
    try:
        import yaml
    except ImportError:
        return set()
    try:
        manifest = yaml.safe_load(open(path, encoding="utf-8")) or {}
    except Exception:
        return set()
    out = set()
    for m in manifest.get("models") or []:
        litellm = m.get("litellm") or {}
        if (m.get("engine", "llama-server") != "llama-server"
                or litellm.get("mode") == "image_generation") and m.get("name"):
            out.add(m["name"])
    return out


def request(method, url, key, body, timeout):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return resp.status, (json.loads(raw) if raw.strip() else None)


def fetch_models(base, key, timeout):
    url = f"{base}/v1/models"
    try:
        status, payload = request("GET", url, key, None, timeout=min(timeout, 20))
    except urllib.error.HTTPError as exc:
        die(f"GET {url} -> HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}")
    except (urllib.error.URLError, TimeoutError) as exc:
        die(f"cannot reach {url}: {getattr(exc, 'reason', exc)}")
    if status != 200 or not isinstance(payload, dict):
        die(f"GET {url} returned {status}: {payload}")
    ids = [m["id"] for m in payload.get("data", []) if m.get("id")]
    if not ids:
        die(f"no models advertised by {url}")
    return ids


def short_error(exc):
    """Pull a human-readable message out of an HTTPError body."""
    body = exc.read().decode(errors="replace")
    try:
        obj = json.loads(body)
        err = obj.get("error", obj)
        msg = err.get("message") if isinstance(err, dict) else err
        body = msg or body
    except Exception:
        pass
    return " ".join(str(body).split())[:300]


def test_one(base, key, model, prompt, max_tokens, timeout):
    """Return (verdict, seconds, detail). verdict in {ok, warn, fail}."""
    url = f"{base}/v1/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
    }
    start = time.monotonic()
    try:
        status, payload = request("POST", url, key, body, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return "fail", time.monotonic() - start, f"HTTP {exc.code}: {short_error(exc)}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return "fail", time.monotonic() - start, f"connection: {getattr(exc, 'reason', exc)}"
    elapsed = time.monotonic() - start

    if status != 200 or not isinstance(payload, dict):
        return "fail", elapsed, f"HTTP {status}: {str(payload)[:300]}"

    choices = payload.get("choices") or []
    if not choices:
        return "fail", elapsed, "200 but no choices in response"
    msg = choices[0].get("message") or {}
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()
    finish = choices[0].get("finish_reason")

    if content:
        return "ok", elapsed, f'reply="{" ".join(content.split())[:60]}"'
    if reasoning:
        return "ok", elapsed, f"thinking only ({len(reasoning)} chars); bump --max-tokens for a final answer"
    return "warn", elapsed, f"empty content (finish_reason={finish}); likely a thinking model — raise --max-tokens"


def parse_args():
    p = argparse.ArgumentParser(
        description="Smoke-test every model an endpoint serves.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("models", nargs="*", help="restrict to these model names (default: all)")
    p.add_argument("--via", choices=["upstream", "litellm"], default="upstream",
                   help="which endpoint to test (default upstream = llama-swap)")
    p.add_argument("--base", help="explicit base URL (overrides --via)")
    p.add_argument("--api-key", dest="api_key", help="explicit api key (overrides --via)")
    p.add_argument("--max-tokens", dest="max_tokens", type=int, default=256)
    p.add_argument("--timeout", type=float, default=120.0, help="per-request timeout (cold start)")
    p.add_argument("--prompt", default="Reply with exactly the word: OK")
    return p.parse_args()


def main():
    args = parse_args()

    if args.base:
        base_url, key = args.base, args.api_key
    elif args.via == "litellm":
        base_url = setting(None, "LITELLM_URL", "http://localhost:4000")
        key = args.api_key or (os.environ.get("LITELLM_API_KEY") or os.environ.get("LITELLM_MASTER_KEY")
                               or DOTENV.get("LITELLM_API_KEY") or DOTENV.get("LITELLM_MASTER_KEY"))
        if not key:
            die("--via litellm needs LITELLM_API_KEY / LITELLM_MASTER_KEY (env or .env).")
    else:
        base_url = setting(None, "UPSTREAM_URL", "http://localhost:11434")
        key = args.api_key

    base = openai_base(base_url)

    print(f"target : {base}/v1   (--via {args.via})")
    print(f"params : max_tokens={args.max_tokens}  timeout={args.timeout:g}s  prompt={args.prompt!r}")

    models = fetch_models(base, key, args.timeout)
    if args.models:
        wanted = set(args.models)
        missing = wanted - set(models)
        if missing:
            print(f"{YELLOW}note{RESET}: not advertised by this endpoint: {sorted(missing)}")
        models = [m for m in models if m in wanted]
        if not models:
            die("none of the requested models are advertised by this endpoint.")
    else:
        # Testing everything: skip non-chat (image) models — they don't speak
        # /v1/chat/completions. Test them explicitly by name if you must.
        skip = image_models()
        hidden = [m for m in models if m in skip]
        if hidden:
            print(f"{DIM}skipping non-chat models: {', '.join(hidden)}{RESET}")
        models = [m for m in models if m not in skip]
    print(f"testing {len(models)} model(s)\n")
    sys.stdout.flush()

    results = []
    width = max(len(m) for m in models)
    for i, model in enumerate(models, 1):
        print(f"[{i}/{len(models)}] {model:<{width}}  ... ", end="", flush=True)
        verdict, secs, detail = test_one(base, key, model, args.prompt, args.max_tokens, args.timeout)
        color = {"ok": GREEN, "warn": YELLOW, "fail": RED}[verdict]
        print(f"{color}{verdict.upper():4}{RESET}  {secs:5.1f}s  {DIM}{detail}{RESET}")
        results.append((model, verdict, detail))

    oks = [m for m, v, _ in results if v == "ok"]
    warns = [(m, d) for m, v, d in results if v == "warn"]
    fails = [(m, d) for m, v, d in results if v == "fail"]

    print(f"\n{'-' * 60}")
    print(f"{len(results)} tested: {GREEN}{len(oks)} ok{RESET}, "
          f"{YELLOW}{len(warns)} warn{RESET}, {RED}{len(fails)} fail{RESET}")
    for m, d in warns:
        print(f"  {YELLOW}warn{RESET} {m}: {d}")
    for m, d in fails:
        print(f"  {RED}fail{RESET} {m}: {d}")

    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
