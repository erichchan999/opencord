FROM node:22-trixie-slim

ARG OPENCODE_VERSION=1.19.5

# Tools the agent (and channel users, through it) can use.
# iptables, iproute2 and util-linux (setpriv) are only needed by the root entrypoint.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git gh jq less procps ripgrep unzip \
      python3 python3-pip python3-venv \
      iptables iproute2 util-linux \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g "opencode-ai@${OPENCODE_VERSION}" && npm cache clean --force

COPY firewall.sh entrypoint.sh start-agent.sh /usr/local/bin/
COPY opencode.json /etc/opencode/opencode.json

# Normalise line endings (files may be edited on Windows), lock down the
# root-owned scripts and config, and pre-create volume mount points owned by
# the unprivileged user so fresh named volumes inherit that ownership.
RUN sed -i 's/\r$//' /usr/local/bin/*.sh /etc/opencode/opencode.json \
 && chmod 755 /usr/local/bin/*.sh \
 && chmod 644 /etc/opencode/opencode.json \
 && mkdir -p /work /home/node/.local/share/opencode /home/node/.config /home/node/.cache \
 && chown -R node:node /work /home/node

ENV OPENCODE_CONFIG=/etc/opencode/opencode.json
WORKDIR /work
EXPOSE 4096

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
  CMD curl -fsS -u "opencode:${OPENCODE_SERVER_PASSWORD}" http://127.0.0.1:4096/global/health >/dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
