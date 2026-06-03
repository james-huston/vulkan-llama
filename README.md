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

## Quick start (on the HX 370, Ubuntu 26.04)

See [docs/research/ubuntu-2604-hx370.md](docs/research/ubuntu-2604-hx370.md) for host prep (IOMMU, groups, `vulkaninfo`).

```bash
cp .env.example .env        # set MODELS_DIR + RENDER_GID/VIDEO_GID (getent group render video)
make models-apply           # download enabled GGUFs + generate config/llama-swap.yaml
docker compose up -d --build
curl http://localhost:11434/v1/models
make sync-litellm           # optional: mirror into LiteLLM
make test-models            # smoke test
```

Workflow and tooling mirror [`battlemage-llama`](../battlemage-llama) — `models.yaml`
is the source of truth; `make models-apply` regenerates the (gitignored)
`config/llama-swap.yaml`. The only backend change is the build (Vulkan) and the
generated `--device Vulkan0` flag.

> **Scaffold status:** Dockerfile (Vulkan), compose, scripts, MoE-first
> `models.yaml`, and the apply→config pipeline are in place and validated
> (dry-run). Pending real-hardware validation + a few AMD adaptations — see
> [story 001](docs/improvements/001-bootstrap-vulkan-llama.md).
