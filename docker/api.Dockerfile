# SOKKAN API — FastAPI + le binaire `claude` (Claude Code CLI) piloté par le SDK.
FROM python:3.12-slim

# Pinned: the image of a release is the same whenever it is built. An unpinned CLI made two
# builds of one tag differ (and a CLI release can change the stream-json the SDK drives).
# Bump BOTH together with backend/requirements.txt (claude-agent-sdk) and
# docker/session.Dockerfile — tests/test_packaging_pins.py checks they agree.
ARG CLAUDE_CODE_VERSION=2.1.322

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates git ripgrep procps openssh-client bubblewrap \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && claude --version | grep -F "${CLAUDE_CODE_VERSION}" \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt /tmp/req-backend.txt
COPY memory/requirements.txt /tmp/req-memory.txt
# docker/constraints.txt = the exact versions of every Python package of the released image
# (direct and transitive), written by `scripts/freeze-constraints.sh` from a built image
COPY docker/constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir -c /tmp/constraints.txt -r /tmp/req-backend.txt -r /tmp/req-memory.txt

COPY VERSION VERSION
COPY backend backend
COPY memory memory
# le package agent host n'est pas exécuté par le container, mais le backend en
# sert install.sh (route /api/magnitude/install.sh) — il doit donc être présent
COPY magnitude magnitude
COPY scripts/api-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# le process tourne en user non-privilégié ; /data (volume nommé) hérite de
# l'ownership de l'image à la première création du volume
RUN groupadd -g 1000 sokkan \
 && useradd -u 1000 -g 1000 -d /data -s /usr/sbin/nologin sokkan \
 && mkdir -p /data && chown -R sokkan:sokkan /data /app

# Embedding model baked into the image (~120 MB): without this, the first
# memory search downloads it from Hugging Face, which breaks an air-gapped
# install and sends a request we do not need. Overriding SOKKAN_EMBED_MODEL at
# runtime falls back to downloading that other model on first use.
RUN mkdir -p /opt/sokkan/models \
 && python -c "from fastembed import TextEmbedding; \
TextEmbedding(model_name='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2', \
cache_dir='/opt/sokkan/models')" \
 && chown -R sokkan:sokkan /opt/sokkan

# commit of the build, served by GET /api/version (a rollout check compares version+commit
# with the dist it deployed, so an old container still answering /api/health is caught)
ARG SOKKAN_COMMIT=unknown
ENV SOKKAN_COMMIT=${SOKKAN_COMMIT}

# conventions container : workspace monté sur /workspace, état sur /data
ENV FASTEMBED_CACHE_PATH=/opt/sokkan/models \
    SOKKAN_DATA_DIR=/data \
    CLAUDE_CONFIG_DIR=/data/claude \
    SOKKAN_AGENT_CWD=/workspace \
    SOKKAN_MEMORY_DIR=/data/claude/projects/-workspace/memory \
    SOKKAN_PROJECT_WD=/workspace \
    HOME=/data
# No SOKKAN_FEATURE_* here: docker-compose.yml passes every switch (empty when unset), which
# overrode the `TMUX=0` this image used to carry — tmux was announced on with no tmux
# installed. A feature that needs a binary (tmux, a browser for Preview) now checks for it
# (backend/features.py `available`).

# démarre root : l'entrypoint rend /etc/hosts writable par le groupe (mode
# managé : le backend y maintient la nomenclature `<name>.fleet`) puis DROP
# immédiatement vers sokkan (setpriv). L'app ne tourne jamais en root.
EXPOSE 8097
ENTRYPOINT ["/entrypoint.sh"]
