# SOKKAN appliance — the image of the local model server: vLLM (Intel XPU build) + the
# « one visible device per worker » fix. Without it, a tensor-parallel worker that sees several
# Arc cards makes the xe driver mirror every GB of VRAM in host RAM (measured 10.11.2026 on
# 4× B60: ~46 GB of host RAM for a 27B model, swap storm). Upstream: vllm-project/vllm #53822,
# PR #56012 (not merged at the time of writing) — drop this layer when it is.
#
#   docker build -t sokkan-ship:<vllm-version> --build-arg BASE=<your vLLM XPU image> deploy/appliance/ship
#
# BASE is a vLLM >= 0.32 XPU image (upstream docker/Dockerfile.xpu); the build fails if the
# patch no longer applies — never run an unpatched server by accident.
ARG BASE=vllm-xpu:0.32.0
FROM ${BASE}
USER root
COPY one-device-per-worker.patch /tmp/one-device-per-worker.patch
RUN set -e; \
    SITE="$(python3 -c 'import vllm, os; print(os.path.dirname(os.path.dirname(vllm.__file__)))')"; \
    (command -v patch >/dev/null || (apt-get update && apt-get install -y --no-install-recommends patch && rm -rf /var/lib/apt/lists/*)); \
    patch -p1 -d "$SITE" < /tmp/one-device-per-worker.patch; \
    python3 -c 'import vllm.v1.executor.multiproc_executor as m, inspect; assert "VLLM_XPU_ONE_DEVICE_PER_WORKER" in inspect.getsource(m)'; \
    rm /tmp/one-device-per-worker.patch
