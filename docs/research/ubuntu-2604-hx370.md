# Ubuntu 26.04 LTS setup notes — Ryzen AI 9 HX 370 (vulkan-llama)

Host-prep specifics for the target box. (Lives under `research/` for now; promote
to `docs/host-setup.md` once validated on real hardware.)

**Target:** Ubuntu 26.04 LTS "Resolute Raccoon" (kernel 7.0) on a Ryzen AI 9
HX 370 — Radeon 890M iGPU (RDNA 3.5, gfx1150), XDNA2 NPU, 96 GB RAM.

## 1. BIOS (do this at install time)

- **IOMMU = Enabled** (AMD CBS → NBIO → IOMMU). **Required for the NPU** (amdxdna);
  harmless for the iGPU. Without it the NPU won't enumerate.
- **Above 4G Decoding = Enabled** and **Re-Size BAR = Enabled** — lets the iGPU map
  large apertures (helps addressing the big unified memory pool).
- **iGPU UMA / Frame Buffer Size**: set generously if offered (e.g. 4–8 GB), but on
  Linux the iGPU also uses **GTT** (dynamic system RAM) beyond UMA, so this isn't
  the hard cap — see §4.

## 2. First boot

```bash
sudo apt update && sudo apt full-upgrade -y      # get linux-firmware ≥ 20260221 (NPU FW ≥1.1.0.0)
sudo usermod -aG render,video "$USER"            # /dev/dri access for GPU compute
# log out/in (or reboot) for group membership
```

## 3. Verify the iGPU via Vulkan (the Phase 1 path)

```bash
sudo apt install -y mesa-vulkan-drivers vulkan-tools
vulkaninfo --summary        # expect: "AMD Radeon 890M (RADV GFX1150)" as a Vulkan device
ls -l /dev/dri/renderD128   # render node must exist
```

If the 890M shows up under `vulkaninfo`, llama.cpp's Vulkan backend will use it.
Kernel 7.0 ships a recent Mesa/RADV (well past the kernel ≥6.11 / Mesa ≥24.3 floor
RDNA 3.5 needs).

## 4. iGPU memory — addressing big models in the 96 GB

The iGPU's usable "VRAM" = the BIOS **UMA** carveout **+ GTT** (dynamically
allocated system RAM). On 96 GB this is the whole point — you can load big MoE /
70B-class models into iGPU-addressable memory.

- Check current GTT/VRAM: `sudo apt install -y amdgpu-top && amdgpu-top` (or
  `radeontop`). Look at GTT total.
- If GTT is too small for a target model, raise it with a kernel param, e.g.
  `amdgpu.gttsize=65536` (64 GB) via `/etc/default/grub` →
  `GRUB_CMDLINE_LINUX_DEFAULT`, then `sudo update-grub && reboot`. (Kernel 7.0
  defaults are usually generous, but verify before loading a 40 GB+ model.)

## 5. Docker (Phase 1 serving)

- Install Docker Engine + Compose plugin.
- The Vulkan iGPU passes through with just `devices: ["/dev/dri:/dev/dri"]` — **no
  special runtime** (unlike NVIDIA). The image bundles `mesa-vulkan-drivers` so the
  RADV ICD is present inside the container. Container user needs the `render`/`video`
  GIDs (set via `group_add` in compose, matching the host — check with
  `getent group render video`).

## 6. NPU (Phase 2 — not needed for Phase 1)

- Kernel 7.0 has `amdxdna` native → `ls /dev/accel/` should show `accel0` (needs
  IOMMU on).
- For FastFlowLM / Lemonade later:
  ```bash
  sudo add-apt-repository ppa:lemonade-team/stable
  sudo apt install libxrt-npu2 amdxdna-dkms   # dkms optional on kernel 7.0
  sudo reboot
  ```
- Verify: `ls /dev/accel` (and `xrt-smi examine` if XRT installed).
- ⚠️ Known **container passthrough bug** for the NPU → run FastFlowLM/Lemonade
  **bare-metal** in Phase 2, not in Docker.

## 7. Monitoring (the `qmassa`/`xpu-smi` equivalent)

- `amdgpu_top` (`amdgpu-top`) — GPU/VRAM/GTT usage, per-process.
- `radeontop` — quick utilization.
- These replace battlemage's `qmassa`/`xpu-smi` for the `make status` target.
