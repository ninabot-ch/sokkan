# SOKKAN API — FastAPI + le binaire `claude` (Claude Code CLI) piloté par le SDK.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates git ripgrep procps \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt /tmp/req-backend.txt
COPY memory/requirements.txt /tmp/req-memory.txt
RUN pip install --no-cache-dir -r /tmp/req-backend.txt -r /tmp/req-memory.txt

COPY backend backend
COPY memory memory
COPY scripts/api-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# le process tourne en user non-privilégié ; /data (volume nommé) hérite de
# l'ownership de l'image à la première création du volume
RUN groupadd -g 1000 sokkan \
 && useradd -u 1000 -g 1000 -d /data -s /usr/sbin/nologin sokkan \
 && mkdir -p /data && chown -R sokkan:sokkan /data /app

# conventions container : workspace monté sur /workspace, état sur /data
ENV SOKKAN_DATA_DIR=/data \
    CLAUDE_CONFIG_DIR=/data/claude \
    SOKKAN_AGENT_CWD=/workspace \
    SOKKAN_MEMORY_DIR=/data/claude/projects/-workspace/memory \
    SOKKAN_FEATURE_PREVIEW=0 \
    SOKKAN_FEATURE_TMUX=0 \
    HOME=/data

USER sokkan
EXPOSE 8097
ENTRYPOINT ["/entrypoint.sh"]
