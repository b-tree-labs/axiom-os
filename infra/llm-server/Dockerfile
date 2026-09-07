# Axiom embedded LLM server — PrismML Bonsai 1.7B (1-bit, 240MB)
#
# Builds PrismML's llama.cpp fork with 1-bit kernel support and bakes
# in the Bonsai 1.7B GGUF model. Serves an OpenAI-compatible API on :8080.
#
# Build:
#   docker build -t axiom-llm-server infra/llm-server/
#
# Run standalone:
#   docker run -p 8080:8080 axiom-llm-server

# ---- builder: compile llama.cpp with 1-bit kernels ----
FROM debian:bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake ninja-build g++ curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Clone PrismML's fork (has 1-bit quantization kernels)
RUN git clone --depth 1 --branch prism \
    https://github.com/PrismML-Eng/llama.cpp.git .

# Build llama-server binary (CPU-only for K3D; GPU builds need different base)
# Static linking avoids shared library issues in the runtime image
RUN cmake -B build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DGGML_NATIVE=OFF \
    -DBUILD_SHARED_LIBS=OFF \
    && cmake --build build --target llama-server -j$(nproc)

# ---- model: download Bonsai 1.7B GGUF ----
FROM debian:bookworm-slim AS model

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /model

# Download the 1.7B model (~248MB) — smallest Bonsai, fits anywhere
RUN curl -fSL -o bonsai-1.7b.gguf \
    "https://huggingface.co/prism-ml/Bonsai-1.7B-gguf/resolve/main/Bonsai-1.7B.gguf"

# ---- runtime: minimal image with binary + model ----
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home axiom

COPY --from=builder /build/build/bin/llama-server /usr/local/bin/llama-server
COPY --from=model /model/bonsai-1.7b.gguf /models/bonsai-1.7b.gguf

USER axiom
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s \
    CMD curl -sf http://localhost:8080/health || exit 1

ENTRYPOINT ["llama-server"]
CMD [ \
    "--model", "/models/bonsai-1.7b.gguf", \
    "--host", "0.0.0.0", \
    "--port", "8080", \
    "--ctx-size", "4096", \
    "--threads", "2", \
    "--parallel", "1" \
]
