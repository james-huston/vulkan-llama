#!/usr/bin/env python3
"""status.py — one-shot snapshot of what llama-swap currently has loaded (AMD/Vulkan).

  - resident model(s) from llama-swap's /running endpoint (name, state, context)
  - that model's llama-server process: host RAM (RSS) + CPU% (docker compose exec ps)
  - GPU VRAM/GTT used + busy% + power, read from the amdgpu sysfs (no extra tools)

Run via `make status`. Endpoint from LLAMA_SWAP_URL / UPSTREAM_URL (default
http://localhost:11434). Override the GPU node with AMDGPU_CARD (e.g. card1) if
auto-detection picks the wrong one.
"""

import glob
import json
import os
import subprocess
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = (os.environ.get("LLAMA_SWAP_URL") or os.environ.get("UPSTREAM_URL")
       or "http://localhost:11434").rstrip("/")


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


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except Exception:
        return None


def find_amdgpu_card():
    """/sys/.../cardN/device for the AMD GPU (vendor 0x1002 with VRAM info)."""
    forced = os.environ.get("AMDGPU_CARD")
    if forced:
        return f"/sys/class/drm/{forced}/device"
    for dev in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
        if _read(os.path.join(dev, "vendor")) == "0x1002" \
                and os.path.exists(os.path.join(dev, "mem_info_vram_total")):
            return dev
    return None


def gpu_stats():
    """VRAM/GTT (MiB), busy %, and power (W) from amdgpu sysfs, or None."""
    dev = find_amdgpu_card()
    if not dev:
        return None

    def mib(name):
        v = _read(os.path.join(dev, name))
        return int(v) / 1024 / 1024 if v and v.isdigit() else None

    s = {k: mib(f"mem_info_{k}") for k in
         ("vram_used", "vram_total", "gtt_used", "gtt_total")}
    busy = _read(os.path.join(dev, "gpu_busy_percent"))
    s["busy"] = float(busy) if busy and busy.replace(".", "", 1).isdigit() else None
    s["power_w"] = None
    for p in (glob.glob(os.path.join(dev, "hwmon/hwmon*/power1_average"))
              + glob.glob(os.path.join(dev, "hwmon/hwmon*/power1_input"))):
        v = _read(p)
        if v and v.isdigit():
            s["power_w"] = int(v) / 1e6
            break
    return s


def _arg(cmd, flag):
    toks = cmd.split()
    return next((toks[i + 1] for i, t in enumerate(toks)
                 if t == flag and i + 1 < len(toks)), "")


def main():
    print("vulkan-llama status")
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
    if gpu and gpu.get("vram_total"):
        vu, vt = gpu["vram_used"] or 0, gpu["vram_total"]
        pct = f" ({vu / vt * 100:.0f}%)" if vt else ""
        line = f"  GPU      : VRAM {vu:.0f} / {vt:.0f} MiB{pct}"
        if gpu.get("gtt_used") is not None and gpu.get("gtt_total"):
            line += f"   GTT {gpu['gtt_used']:.0f} / {gpu['gtt_total']:.0f} MiB"
        print(line)
        extra = []
        if gpu.get("busy") is not None:
            extra.append(f"busy {gpu['busy']:.0f}%")
        if gpu.get("power_w") is not None:
            extra.append(f"{gpu['power_w']:.0f} W")
        if extra:
            print("             " + "   ".join(extra)
                  + "   (point-in-time sample)")
    else:
        print("  GPU      : (amdgpu sysfs not found — set AMDGPU_CARD=cardN)")


if __name__ == "__main__":
    main()
