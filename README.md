# vulkan-llama

A self-hosted, OpenAI-API-compatible local LLM stack for **AMD APUs**, driving the
integrated Radeon GPU via **llama.cpp's Vulkan backend** + [`llama-swap`](https://github.com/mostlygeek/llama-swap).
Sibling to [`battlemage-llama`](../battlemage-llama) (Intel/SYCL) and
[`rocm-llama`](../rocm-llama) (AMD discrete/ROCm).

**Target hardware:** Ryzen AI 9 HX 370 (Strix Point) — Radeon 890M iGPU (RDNA 3.5,
gfx1150), XDNA2 NPU, 96 GB unified RAM.

## Status

🚧 **Bootstrapping.** See the plan: [docs/improvements/001-bootstrap-vulkan-llama.md](docs/improvements/001-bootstrap-vulkan-llama.md).
Target OS: **Ubuntu 26.04 LTS** (kernel 7.0, native `amdxdna` NPU driver).

## Why Vulkan (not ROCm) on this iGPU

ROCm doesn't officially support gfx1150, and real-world reports put it *below
CPU speed* on the 890M. Vulkan (Mesa RADV) is the stable, recommended path. The
**96 GB unified memory** is the real advantage — run big MoE models that won't
fit a 32 GB discrete card. Decode is memory-bandwidth-bound (~120 GB/s), so the
model strategy is **MoE-first**.

## The NPU

The HX 370's XDNA2 NPU became Linux-usable for LLMs in **March 2026** (Lemonade +
FastFlowLM). It's planned as a **Phase 2 complement** to benchmark against the
iGPU — see [docs/research/npu-xdna2-2026-06.md](docs/research/npu-xdna2-2026-06.md).
