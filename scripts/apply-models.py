#!/usr/bin/env python3
"""
apply-models.py

Make the live llama-swap config a pure function of models.yaml (the source of
truth). For each ENABLED model it downloads the GGUF if missing, then
regenerates config/llama-swap.yaml from scratch. Disabled entries are ignored.

    make models-apply            # download missing + regenerate the config
    make models-apply DRY_RUN=1  # show the plan + generated config, write nothing

Workflow: edit models.yaml -> `make models-apply` -> `make sync-litellm` ->
`make test-models`.

config/llama-swap.yaml is overwritten (a .bak is kept). Edit models.yaml, not
the generated config. config/llama-swap.example.yaml stays as hand-written docs.

Downloads reuse scripts/add-model.sh (hf CLI or curl). MODELS_DIR is resolved
from the env var, then .env, then the repo default /opt/apps/ollama-models.
"""

import argparse
import os
import shutil
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("error: PyYAML is required. Install it with: pip install pyyaml")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(REPO_ROOT, ".env")
ADD_MODEL = os.path.join(REPO_ROOT, "scripts", "add-model.sh")
TEMPLATES_DIR = os.path.join(REPO_ROOT, "templates")
DEFAULT_MANIFEST = os.path.join(REPO_ROOT, "models.yaml")
DEFAULT_CONFIG = os.path.join(REPO_ROOT, "config", "llama-swap.yaml")

DEFAULT_CTX = 131072
DEFAULT_TTL = 600
DEFAULT_HEALTHCHECK = 180
DEFAULT_START_PORT = 12800
SCHEMA = ("# yaml-language-server: $schema="
          "https://raw.githubusercontent.com/mostlygeek/llama-swap/refs/heads/main/config-schema.json")


def die(msg):
    sys.exit(f"error: {msg}")


def load_dotenv(path):
    values = {}
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def resolve_models_dir(dotenv):
    if os.environ.get("MODELS_DIR"):
        return os.environ["MODELS_DIR"]
    if dotenv.get("MODELS_DIR"):
        return dotenv["MODELS_DIR"]
    return "/opt/apps/ollama-models"


def model_path(entry):
    """Container path for --model, plus the host-relative path under MODELS_DIR."""
    if entry.get("path"):
        cpath = entry["path"]
        rel = cpath[len("/models/"):] if cpath.startswith("/models/") else None
        return cpath, rel
    repo = entry.get("repo")
    file = entry.get("file")
    out = entry.get("out")
    if not repo:
        die(f"model '{entry.get('name')}' needs either 'path' or 'repo' (+ 'file' for Hugging Face).")
    is_civitai = repo.startswith("civitai:")
    if not file and not (is_civitai and out):
        die(f"model '{entry.get('name')}' needs 'file' "
            f"(or, for civitai, 'out:' so the local filename is known).")
    dir_ = entry.get("dir") or entry["name"]
    local_name = out or os.path.basename(file)
    rel = f"{dir_}/{local_name}"
    return f"/models/{rel}", rel


def generate_block(entry, cpath):
    name = entry["name"]
    engine = entry.get("engine", "llama-server")
    lines = [f"  # {name} — from models.yaml", f"  {name}:", "    cmd: |"]

    if engine == "sd-server":
        # stable-diffusion.cpp image server. llama-swap injects ${PORT}; it
        # proxies /v1/images/generations to this upstream.
        # NOTE: --diffusion-fa was disabled on Battlemage/SYCL (crashed). On the
        # Vulkan/RDNA3.5 path it is untested — opt in per-model via
        # `extra: --diffusion-fa` once validated on the 890M.
        lines += [
            "      /opt/sd-cpp/bin/sd-server",
            f"      --model {cpath}",
            "      --listen-ip 127.0.0.1",
            "      --listen-port ${PORT}",
        ]
        if entry.get("threads"):
            lines.append(f"      --threads {entry['threads']}")
        if entry.get("extra"):
            lines.append(f"      {entry['extra']}")
    else:
        lines += [
            "      /opt/llama-cpp/bin/llama-server",
            "      --port ${PORT}",
            f"      --model {cpath}",
            f"      --alias {name}",
            "      --host 127.0.0.1",
            "      -ngl 99",
            f"      -c {entry.get('ctx', DEFAULT_CTX)}",
            f"      --device {entry.get('device', 'Vulkan0')}",
            "      -sm none",
            "      --jinja",
        ]
        if entry.get("template"):
            lines.append(f"      --chat-template-file /templates/{entry['template']}")
        if entry.get("reasoning_format"):
            lines.append(f"      --reasoning-format {entry['reasoning_format']}")
        elif entry.get("reasoning"):
            lines.append("      --reasoning-format deepseek")
        if entry.get("temp") is not None:
            lines.append(f"      --temp {entry['temp']}")
        if entry.get("top_p") is not None:
            lines.append(f"      --top-p {entry['top_p']}")
        if entry.get("extra"):
            lines.append(f"      {entry['extra']}")

    if engine == "sd-server":
        # llama-swap's default health probe is /health, which sd-server doesn't
        # serve; point it at an endpoint sd-server answers once the model is up.
        lines.append("    checkEndpoint: /v1/models")
    lines.append(f"    ttl: {entry.get('ttl', DEFAULT_TTL)}")
    return "\n".join(lines)


def build_config(manifest, enabled):
    health = manifest.get("healthCheckTimeout", DEFAULT_HEALTHCHECK)
    start = manifest.get("startPort", DEFAULT_START_PORT)
    head = [
        SCHEMA,
        "#",
        "# GENERATED by scripts/apply-models.py from models.yaml — DO NOT EDIT BY HAND.",
        "# Edit models.yaml and run `make models-apply`.",
        "",
        f"healthCheckTimeout: {health}",
        f"startPort: {start}",
        "",
        "models:",
        "",
    ]
    blocks = []
    for entry, cpath, _ in enabled:
        blocks.append(generate_block(entry, cpath))
    return "\n".join(head) + "\n\n".join(blocks) + "\n"


def download_if_missing(entry, models_dir, rel, dry_run):
    """Return 'have' | 'get' | 'missing-path'. Downloads via add-model.sh."""
    if entry.get("path"):
        # Pre-installed file / Ollama blob — just sanity-check presence.
        if rel and not os.path.isfile(os.path.join(models_dir, rel)):
            return "missing-path"
        return "have"

    host_path = os.path.join(models_dir, rel)
    if os.path.isfile(host_path) and os.path.getsize(host_path) > 0:
        return "have"
    if dry_run:
        return "get"

    cmd = ["bash", ADD_MODEL, "--no-config",
           "--repo", entry["repo"],
           "--name", entry["name"], "--dir", entry.get("dir") or entry["name"]]
    if entry.get("file"):
        cmd += ["--file", entry["file"]]
    if entry.get("out"):
        cmd += ["--out", entry["out"]]
    if entry.get("branch"):
        cmd += ["--branch", entry["branch"]]
    env = dict(os.environ, MODELS_DIR=models_dir)
    subprocess.run(cmd, check=True, env=env)
    return "get"


def find_orphans(models_dir, enabled):
    """GGUF dirs under MODELS_DIR not referenced by any enabled entry (report only)."""
    referenced = set()
    for _, _, rel in enabled:
        if rel:
            referenced.add(rel.split("/", 1)[0])
    if not os.path.isdir(models_dir):
        return []
    orphans = []
    for name in sorted(os.listdir(models_dir)):
        full = os.path.join(models_dir, name)
        if name in ("blobs", "manifests") or not os.path.isdir(full):
            continue
        if name in referenced:
            continue
        if any(f.endswith(".gguf") for f in os.listdir(full)):
            orphans.append(name)
    return orphans


def parse_args():
    p = argparse.ArgumentParser(description="Apply models.yaml to the llama-swap config.")
    p.add_argument("--manifest", default=DEFAULT_MANIFEST)
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--dry-run", action="store_true", help="show plan + config, write nothing")
    p.add_argument("--no-download", action="store_true", help="regenerate config only")
    return p.parse_args()


def main():
    args = parse_args()
    dotenv = load_dotenv(ENV_FILE)
    models_dir = resolve_models_dir(dotenv)

    if not os.path.isfile(args.manifest):
        die(f"manifest not found: {args.manifest}")
    with open(args.manifest, encoding="utf-8") as fh:
        manifest = yaml.safe_load(fh) or {}
    all_models = manifest.get("models") or []
    if not all_models:
        die(f"no models defined in {args.manifest}")

    enabled = []
    seen = set()
    for entry in all_models:
        name = entry.get("name")
        if not name:
            die("a model entry is missing 'name'.")
        if name in seen:
            die(f"duplicate model name in manifest: {name}")
        seen.add(name)
        if entry.get("enabled", True) is False:
            continue
        if entry.get("template") and not os.path.isfile(os.path.join(TEMPLATES_DIR, entry["template"])):
            print(f"warning: templates/{entry['template']} not found (referenced by {name})")
        cpath, rel = model_path(entry)
        enabled.append((entry, cpath, rel))

    disabled = [m["name"] for m in all_models if m.get("enabled", True) is False]
    print(f"MODELS_DIR = {models_dir}")
    print(f"manifest   = {args.manifest}")
    print(f"enabled    = {len(enabled)}   disabled = {len(disabled)} {disabled if disabled else ''}")
    if args.dry_run:
        print("mode       = DRY RUN — nothing will be downloaded or written")
    print()

    # 1. Downloads
    if not args.no_download:
        for entry, _, rel in enabled:
            status = download_if_missing(entry, models_dir, rel, args.dry_run)
            mark = {"have": "have", "get": "WOULD DOWNLOAD" if args.dry_run else "downloaded",
                    "missing-path": "MISSING (path not found on host!)"}[status]
            print(f"  [{mark}] {entry['name']}")
        print()

    # 2. Regenerate config
    content = build_config(manifest, enabled)
    if args.dry_run:
        print(f"=== would write {args.config} ===")
        print(content)
    else:
        os.makedirs(os.path.dirname(args.config), exist_ok=True)
        if os.path.isfile(args.config):
            shutil.copy2(args.config, args.config + ".bak")
        with open(args.config, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"wrote {args.config} ({len(enabled)} models)"
              + (f"  [backup: {os.path.basename(args.config)}.bak]" if os.path.isfile(args.config + ".bak") else ""))

    # 3. Orphan report (informational; never deletes files)
    orphans = find_orphans(models_dir, enabled)
    if orphans:
        print()
        print("note: GGUF dirs in MODELS_DIR not referenced by any enabled model:")
        for o in orphans:
            print(f"  - {o}  (delete manually if no longer wanted)")

    if not args.dry_run:
        print()
        print("Next: `make sync-litellm` to mirror into LiteLLM, then "
              "`make test-models` to verify.")


if __name__ == "__main__":
    main()
