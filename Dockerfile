# syntax=docker/dockerfile:1.6
#
# vulkan-llama — Vulkan-accelerated llama.cpp + llama-swap for AMD APUs/GPUs
# (Radeon 890M / RDNA 3.5 and friends). Driver-agnostic: the RADV (Mesa) Vulkan
# ICD inside the container talks to the host amdgpu kernel driver via /dev/dri —
# no ROCm toolkit, no kernel modules beyond the stock amdgpu.
#
# Build:  docker compose up -d --build
# Docs:   https://github.com/james-huston/vulkan-llama

# Newer Ubuntu base = newer Mesa/RADV (RDNA 3.5 / gfx1150 needs Mesa >= 24.3).
# 26.04 matches the recommended host; drop to 25.10 if 26.04 isn't pullable yet.
ARG UBUNTU_TAG=26.04
FROM ubuntu:${UBUNTU_TAG}

ARG DEBIAN_FRONTEND=noninteractive
# Build deps + Vulkan build bits (loader, headers, glsl compiler for the compute
# shaders) + the RADV runtime ICD (mesa-vulkan-drivers) so the container can
# actually drive the iGPU. vulkan-tools gives `vulkaninfo` for diagnostics.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git cmake ninja-build build-essential pkg-config ca-certificates curl wget \
        libvulkan-dev glslc glslang-tools spirv-headers spirv-tools vulkan-tools \
        mesa-vulkan-drivers libvulkan1 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# Build llama.cpp with the Vulkan backend.
#   -DGGML_VULKAN=ON  — Vulkan compute backend (RADV on Mesa)
# Pin a tag for reproducible builds:  --build-arg LLAMA_CPP_REF=b9409
# -----------------------------------------------------------------------------
ARG LLAMA_CPP_REF=master
RUN git clone --depth 1 --branch ${LLAMA_CPP_REF} \
        https://github.com/ggml-org/llama.cpp.git /tmp/llama.cpp && \
    cd /tmp/llama.cpp && \
    cmake -B build -G Ninja \
        -DGGML_VULKAN=ON \
        -DGGML_NATIVE=ON \
        -DCMAKE_BUILD_TYPE=Release && \
    cmake --build build --config Release -j "$(nproc)" && \
    cmake --install build --prefix=/opt/llama-cpp && \
    echo "/opt/llama-cpp/lib" > /etc/ld.so.conf.d/llama-cpp.conf && ldconfig && \
    rm -rf /tmp/llama.cpp

# NOTE: stable-diffusion.cpp (sd-server) with -DSD_VULKAN=ON can be added here
# later for image generation (engine: sd-server in models.yaml). Left out of the
# Phase-1 LLM-focused build to keep it lean and fast.

# -----------------------------------------------------------------------------
# llama-swap — model-swap proxy on :11434 (OpenAI-compatible, hot config reload)
# -----------------------------------------------------------------------------
ARG LLAMA_SWAP_VERSION=217
RUN wget -qO /tmp/llama-swap.tar.gz \
        "https://github.com/mostlygeek/llama-swap/releases/download/v${LLAMA_SWAP_VERSION}/llama-swap_${LLAMA_SWAP_VERSION}_linux_amd64.tar.gz" && \
    mkdir -p /opt/llama-swap/bin && \
    tar -C /opt/llama-swap/bin -xzf /tmp/llama-swap.tar.gz llama-swap && \
    rm /tmp/llama-swap.tar.gz && \
    /opt/llama-swap/bin/llama-swap --version

ENV PATH=/opt/llama-swap/bin:/opt/llama-cpp/bin:${PATH}

# Restrict the Vulkan device set to the first GPU (the 890M). The generated
# config also passes `--device Vulkan0`; both target the iGPU.
ENV GGML_VK_VISIBLE_DEVICES=0

# llama-swap listens on Ollama's port so existing clients/LiteLLM need no change.
EXPOSE 11434

CMD ["/opt/llama-swap/bin/llama-swap", \
     "--config", "/config/llama-swap.yaml", \
     "--listen", "0.0.0.0:11434", \
     "--watch-config"]
