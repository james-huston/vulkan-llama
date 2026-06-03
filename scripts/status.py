#!/usr/bin/env python3
"""status.py — one-shot snapshot of what llama-swap currently has loaded.

Fills the gap qmassa leaves (it shows GPU stats but not *which* model is loaded):
  - resident model(s) from llama-swap's /running endpoint (name, state, context)
  - that model's llama-server process: host RAM (RSS) + CPU% (docker compose exec ps)
  - GPU power draw + VRAM used (xpu-smi; no sudo needed on this rig)

Run via `make status`. Endpoint from LLAMA_SWAP_URL / UPSTREAM_URL (default
http://localhost:11434). Total VRAM for the % from VRAM_TOTAL_MIB (default 32768,
the B70's 32 GB).
"""

import json
import os
import shutil
import subprocess
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = (os.environ.get("LLAMA_SWAP_URL") or os.environ.get("UPSTREAM_URL")
       or "http://localhost:11434").rstrip("/")
VRAM_TOTAL = float(os.environ.get("VRAM_TOTAL_MIB", "32768"))


def get_running():
    try:
        with urllib.request.urlopen(f"{URL}/running", timeout=8) as r:
            return (json.loads(r.read().decode()) or {}).get("running") or []
    except Exception as exc:
        print(f"  loaded   : (could not reach {URL}/running — container down? {exc})")
        return None


def llama_server_procs():
    """{alias: (rss_kb, cpu_pct)} for llama-server processes inside the container."""
    try:
        out = subprocess.run(
            ["docker", "compose", "exec", "-T", "llama-swap", "ps", "-eo", "rss,pcpu,args"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return {}
    procs = {}
    for line in out.splitlines():
        if "llama-server" not in line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        rss, cpu, args = parts
        toks = args.split()
        alias = next((toks[i + 1] for i, t in enumerate(toks)
                      if t == "--alias" and i + 1 < len(toks)), "")
        try:
            procs[alias] = (int(rss), float(cpu))
        except ValueError:
            pass
    return procs


def gpu_stats():
    """(power_w, vram_used_mib) via xpu-smi, or None if unavailable."""
    if not shutil.which("xpu-smi"):
        return None
    try:
        out = subprocess.run(
            ["xpu-smi", "dump", "-d", "0", "-m", "1,18", "-i", "1", "-n", "1"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    for line in reversed(out.splitlines()):
        cells = [c.strip() for c in line.split(",")]
        if len(cells) >= 4:
            try:
                return float(cells[2]), float(cells[3])
            except ValueError:
                continue
    return None


def _arg(cmd, flag):
    toks = cmd.split()
    return next((toks[i + 1] for i, t in enumerate(toks)
                 if t == flag and i + 1 < len(toks)), "")


def main():
    print("battlemage-llama status")
    print(f"  endpoint : {URL}")

    running = get_running()
    procs = llama_server_procs()

    if running == []:
        print("  loaded   : (none — no model resident; VRAM should be free)")
    elif running:
        for r in running:
            name = r.get("model", "?")
            cmd = r.get("cmd", "")
            print(f"  loaded   : {name}  [{r.get('state', '?')}]  ctx={_arg(cmd, '-c')}")
            print(f"             {_arg(cmd, '--model')}")
            if name in procs:
                rss_kb, cpu = procs[name]
                print(f"             host RAM {rss_kb / 1024:.0f} MiB   CPU {cpu:.0f}%")

    gpu = gpu_stats()
    if gpu:
        power, vram = gpu
        pct = f"  ({vram / VRAM_TOTAL * 100:.0f}%)" if VRAM_TOTAL else ""
        print(f"  GPU      : {power:.0f} W   VRAM {vram:.0f} MiB / {VRAM_TOTAL:.0f} MiB{pct}")
        print("             (power is a point-in-time sample; ~3 W idle, 200 W+ under load)")
    else:
        print("  GPU      : (xpu-smi unavailable — run qmassa for live GPU stats)")


if __name__ == "__main__":
    main()
