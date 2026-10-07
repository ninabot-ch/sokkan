# TEST ONLY (k3d-runner-smoke.sh): a ~180 MB stand-in for the session image when the host has
# no room for the real images — same supervisor / relay / egress scripts, a scripted fake CLI
# (tests/fixtures/fake_claude_cli.py) as `claude`, and the runner package for the driver pod.
FROM python:3.12-slim
COPY backend/runner/pod/supervisor.py /usr/local/bin/sokkan-session-supervisor
COPY backend/runner/pod/mcp_relay.py /usr/local/bin/sokkan-mcp-relay
COPY backend/runner/pod/egress_proxy.py /usr/local/bin/sokkan-egress-proxy
COPY tests/fixtures/fake_claude_cli.py /usr/local/bin/claude
COPY backend/runner /opt/sokkan/runner
COPY deploy/helm/sokkan/ci/runner-smoke-driver.py /opt/sokkan/driver.py
RUN sed -i '1s|.*|#!/usr/local/bin/python3|' /usr/local/bin/sokkan-* /usr/local/bin/claude \
 && chmod 0755 /usr/local/bin/sokkan-* /usr/local/bin/claude \
 && mkdir -p /home/session && chgrp 0 /home/session && chmod g=u /home/session
ENV HOME=/home/session
USER 1000
