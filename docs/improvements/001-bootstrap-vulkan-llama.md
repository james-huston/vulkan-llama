# 001 — Bootstrap vulkan-llama on the Ryzen AI 9 HX 370

**Status:** Draft · **Created:** 2026-06-03

## Goal

Stand up a self-hosted, OpenAI-API-compatible local LLM server on the **Ryzen AI
9 HX 370** (Strix Point, Radeon 890M iGPU, XDNA2 NPU, **96 GB RAM**) that gives
the same ergonomics as [`battlemage-llama`](../../../battlemage-llama):
hot-swappable models, an OpenAI `/v1` endpoint, declarative `models.yaml`, and
LiteLLM sync — but driven by the **890M iGPU via llama.cpp Vulkan** instead of
Intel SYCL.

Sibling repos this is modeled on: `battlemage-llama` (Intel/SYCL) and
`rocm-llama` (AMD discrete/ROCm). This one targets the **AMD APU** path.

## Why this hardware is worth it

- **96 GB unified memory** → run models that won't fit a 32 GB discrete card
  (big MoE, even 70B Q4). Capacity is the superpower.
- The 890M (16 CU, RDNA 3.5) is a real iGPU — unlike the 7900X's 2-CU Raphael.
- **Decode is memory-bandwidth-bound** (~120 GB/s, 128-bit LPDDR5x). The iGPU's
  big win is **prefill**; decode is moderate. ⇒ **favor MoE models** (low active
  params dodge the bandwidth limit) and lean on the RAM for capacity.

## Backend decision (settled)

**Use llama.cpp Vulkan, not ROCm**, on the 890M. ROCm doesn't officially support
gfx1150, and a real-world report had ROCm at 6.4 tok/s vs CPU 15.9 tok/s on this
iGPU — *slower than the CPU*. Vulkan (Mesa RADV) is the community-recommended,
stable path and exposes the full iGPU memory pool. See [the NPU research](../research/npu-xdna2-2026-06.md)
for the broader landscape.

## The open architecture decision: server layer

Two viable designs for the serving layer. **This is the main thing to decide.**

### Design A — llama-swap + llama.cpp Vulkan  *(mirrors battlemage)*
- Familiar: reuse `battlemage-llama`'s `models.yaml` → `make models-apply` →
  `llama-swap` → `make sync-litellm` workflow almost verbatim.
- GGUF-flexible (the whole Hugging Face GGUF ecosystem).
- iGPU-only; the **NPU is not used** (add later as a separate endpoint if wanted).
- Lowest risk, fastest to a working box.

### Design B — Lemonade Server  *(unified iGPU + NPU)*
- One OpenAI endpoint brokering **both** llama.cpp Vulkan (GGUF on iGPU) **and**
  FastFlowLM (NPU). Cross-platform, actively maintained (v10.6.0, 2026-05-21).
- Gets the **NPU "for free"** behind the same API.
- Costs: new tool to learn; NPU has a **curated model zoo** (not arbitrary GGUF);
  NPU-in-Docker is buggy (#1771); diverges from the battlemage tooling
  (`models.yaml` / LiteLLM-sync) we'd have to re-create or wrap.

### Recommendation: phase it (Design A now, evaluate B/NPU next)

Start with **Design A** to de-risk and get a dependable GGUF workhorse on the
iGPU, reusing everything we already know. Treat the NPU (and possibly Lemonade as
a unified server) as a **tracked Phase 2 experiment** — because the research found
**no head-to-head NPU-vs-Vulkan benchmark**, so adopting it should be evidence-led.

## Phased plan

### Phase 1 — Vulkan iGPU workhorse  *(primary deliverable)*
- [ ] **OS:** Linux bare-metal — **Ubuntu 26.04 LTS "Resolute Raccoon"** (kernel
      7.0, native `amdxdna` NPU driver; LTS support). Enable **IOMMU** in BIOS and
      update `linux-firmware` (≥20260221) at install. See OS note below.
- [ ] Port the `battlemage-llama` scaffolding: `docker-compose.yml`, `Makefile`,
      `scripts/` (apply-models, sync-litellm, status, test-models), `models.yaml`,
      `tests/`, docs structure.
- [ ] **Dockerfile**: build llama.cpp with `-DGGML_VULKAN=ON` (Mesa RADV /
      Vulkan SDK) instead of SYCL; bundle `llama-swap`. Pass `/dev/dri` through.
- [ ] **`models.yaml`**: MoE-first lineup (Qwen3-30B-A3B, Qwen3.6-35B-A3B,
      gpt-oss-20B, plus a dense coder); exploit the 96 GB for capacity.
- [ ] Verify: model loads on the 890M (Vulkan device shows up), OpenAI `/v1`
      responds, llama-swap hot-swap works, LiteLLM sync works.
- [ ] **Benchmark** decode-vs-context and tok/s per model (llama-bench), like we
      did for the B70 — sets the baseline the NPU must beat.

### Phase 2 — NPU evaluation  *(complement; evidence-led)*
- [ ] Install the Linux NPU stack: `ppa:lemonade-team/stable` →
      `libxrt-npu2 amdxdna-dkms`; enable **IOMMU**; firmware ≥1.1.0.0; memlock.
- [ ] Stand up **Lemonade / FastFlowLM** (likely bare-metal due to the container
      bug) and confirm the NPU `/v1` endpoint answers.
- [ ] **Benchmark NPU vs 890M-Vulkan vs CPU** on the same model/quant (7B–14B) —
      fill the research gap. Record decode + prefill + TTFT.
- [ ] Find the largest practical NPU model; test tool-calling / long context under
      opencode + LiteLLM.
- [ ] **Decide:** (i) keep llama-swap + add the NPU as a LiteLLM-routed side
      endpoint for models where it wins, or (ii) migrate the whole server to
      Lemonade (unified iGPU+NPU). Record the call here.

## Decisions & constraints (carry-overs)

- **OS = Ubuntu 26.04 LTS bare-metal** (kernel 7.0 → native `amdxdna`; LTS; the
  lemonade PPA supports it for Phase 2). Chosen over 25.10 (near-EOL) and over an
  LTS-with-frozen-old-kernel. Vulkan iGPU works in Docker via `/dev/dri`, but the
  NPU needs IOMMU + amdxdna and has a container passthrough bug, so bare-metal
  keeps the Phase-2 NPU door open. WSL2 is iGPU-only; NPU-in-WSL2 is unproven.
  (Windows-on-host would unlock AMD's Hybrid NPU mode but loses the Docker/llama-swap
  workflow — not chosen.)
- **MoE-first** model strategy (bandwidth-bound decode).
- Repo named **`vulkan-llama`** (the API is "Vulkan").

## Open questions

- Adopt Lemonade as the unified server, or keep llama-swap and bolt the NPU on the
  side? (decide in Phase 2 with benchmarks)
- Does `llama-swap` need any change for Vulkan, or is it backend-agnostic? (expect
  agnostic — it just launches `llama-server`.)
- How much of the 96 GB can the iGPU address as VRAM under Linux (GTT sizing)?
