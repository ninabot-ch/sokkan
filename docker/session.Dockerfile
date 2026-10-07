# SOKKAN session image — what ONE session / agent run executes in when SOKKAN_RUNNER is
# `docker` or `kubernetes`: the Claude Code CLI, git and basic tools, the session supervisor,
# the MCP relay and the egress proxy (backend/runner/pod, stdlib Python). The api keeps its own
# image (docker/api.Dockerfile). Non-root; works under an ARBITRARY uid (OpenShift restricted
# SCC): everything it writes is $HOME (=/home/session, an emptyDir / tmpfs in practice) and
# /tmp, group 0 owns them with the owner's rights.
#
#   docker build -f docker/session.Dockerfile -t sokkan-session:local .
#
# The CLI comes from the claude-agent-sdk wheel (its bundled native binary), the SAME source
# as the api's SDK: pin CLAUDE_AGENT_SDK_VERSION to the api's to keep them in step.
ARG CLAUDE_AGENT_SDK_VERSION=
FROM python:3.12-slim AS cli
ARG CLAUDE_AGENT_SDK_VERSION
RUN pip install --no-cache-dir "claude-agent-sdk${CLAUDE_AGENT_SDK_VERSION:+==$CLAUDE_AGENT_SDK_VERSION}" \
 && cp "$(python -c 'import claude_agent_sdk,os;print(os.path.dirname(claude_agent_sdk.__file__))')/_bundled/claude" /claude \
 && /claude --version

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git openssh-client ripgrep procps less jq curl python3 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=cli /claude /usr/local/bin/claude
COPY backend/runner/pod/supervisor.py /usr/local/bin/sokkan-session-supervisor
COPY backend/runner/pod/mcp_relay.py /usr/local/bin/sokkan-mcp-relay
COPY backend/runner/pod/egress_proxy.py /usr/local/bin/sokkan-egress-proxy
RUN sed -i '1s|.*|#!/usr/bin/python3|' /usr/local/bin/sokkan-session-supervisor \
      /usr/local/bin/sokkan-mcp-relay /usr/local/bin/sokkan-egress-proxy \
 && chmod 0755 /usr/local/bin/sokkan-* \
 && useradd -u 1000 -g 0 -d /home/session -s /bin/bash -M session \
 && mkdir -p /home/session/.claude /workspace \
 && chgrp -R 0 /home/session /workspace && chmod -R g=u /home/session /workspace \
 && git config --system --add safe.directory '*'
ENV HOME=/home/session \
    CLAUDE_CONFIG_DIR=/home/session/.claude \
    DISABLE_AUTOUPDATER=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 1000
WORKDIR /workspace
EXPOSE 7070
CMD ["sokkan-session-supervisor"]
