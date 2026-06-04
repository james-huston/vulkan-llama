# NPU benchmark: XDNA2 vs 890M-Vulkan vs B70 (2026-06-04)

**Question:** is the Ryzen AI 9 HX 370's **XDNA2 NPU** worth it for LLM inference,
vs the same chip's **Radeon 890M iGPU** (llama.cpp Vulkan) and our **Arc Pro B70**
(SYCL)? This tests the [NPU research](npu-xdna2-2026-06.md) prediction (~18 t/s)
against reality.

**Verdict: no — the NPU is the slowest of the three on both a MoE and a dense model.**
On the same chip the **iGPU is ~4–5× faster than the NPU** (28.4 vs 6.8 MoE; 16.5 vs
3.4 dense), and the NPU's **prefill is catastrophic** (~10 t/s vs the iGPU's 350–635).
The research's ~18 t/s reproduced on neither model. Dense did **not** help — it was
worse. Not a usable LLM decode engine on current FastFlowLM.

## Setup

- **NPU:** FastFlowLM (`flm`) v0.9.43 on Ubuntu 26.04 / kernel 7.0 (native `amdxdna`,
  FW 1.1.2.64), `libxrt-npu2`, memlock unlimited. `flm serve <model>` → OpenAI API on
  `:52625`. Numbers are FLM's own `usage.prefill_speed_tps` / `decoding_speed_tps`
  (authoritative), `--pmode turbo`. NPU model zoo is curated (not arbitrary GGUF).
- **890M iGPU:** llama.cpp Vulkan (RADV, Mesa 26.0.3), `llama-bench -d Vulkan0`, Q4 GGUF.
- **B70:** llama.cpp SYCL, `llama-bench -d SYCL0`, same GGUF.

## Results

### `gpt-oss-20b` (20B, **A3.6B MoE**, MXFP4/Q4) — decode tok/s

| Backend | hardware | prefill (pp512) | **decode (tg128)** | vs NPU decode |
|---|---|---|---|---|
| **B70** | discrete Arc, GDDR ~456 GB/s | 1356 | **52.9** | 7.8× |
| **890M** | iGPU, LPDDR5x ~120 GB/s (Vulkan) | 635 | **28.4** | 4.2× |
| **NPU** | XDNA2 (FastFlowLM, turbo) | ~10 | **6.8** | 1× |

NPU `performance` pmode ≈ 6.3 t/s; `turbo` ≈ 6.8 — power mode barely matters.

### `llama3.1:8b` (8B, **dense**, Q4_K_M) — decode tok/s

| Backend | hardware | prefill (pp512) | **decode (tg128)** | vs NPU decode |
|---|---|---|---|---|
| **B70** | discrete Arc (SYCL) | 2964 | **85.0** | 25× |
| **890M** | iGPU (Vulkan) | 353 | **16.5** | 4.9× |
| **NPU** | XDNA2 (FastFlowLM, turbo) | ~11 | **3.4** | 1× |

**The dense test did NOT redeem the NPU — it was *worse*** (3.4 vs its own 6.8 on the
MoE). Reason: the NPU is also active-param/bandwidth-limited, so the MoE's 4B active
beats dense 8B even there. The research's ~18 t/s reproduced on **neither** model.

## Takeaways

- On the **same chip**, the iGPU (Vulkan) beats the NPU by **~4× on the MoE and ~5×
  on dense**, and prefills **30–60× faster**. The NPU is the *slowest* of the three
  on every test, and dense made it *worse*. Not redeemable on current FastFlowLM.
- The B70 is the speed king (≈2× the iGPU, 8–25× the NPU).
- **MoE > dense on bandwidth-limited memory** (both iGPU and NPU): low active params
  win — gpt-oss MoE beats llama-3.1 dense on the iGPU (28.4 vs 16.5) and NPU (6.8 vs 3.4).
- The HX 370 box's only real edge stays **capacity** (73 GB addressable: 48 GB UMA
  VRAM + GTT, vs the B70's 32 GB) — run models the B70 can't hold — at ~half B70 speed.
- The NPU may still have niche value (low power, concurrent with the GPU, Whisper/
  embeddings), but **not as a fast LLM decode engine** on current FastFlowLM.

See also: [`docs/improvements/001-bootstrap-vulkan-llama.md`](../improvements/001-bootstrap-vulkan-llama.md)
(Phase 1 iGPU results) and [`npu-xdna2-2026-06.md`](npu-xdna2-2026-06.md) (the research this tests).
