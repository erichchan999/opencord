# Built from the repo root (see compose.yaml), so paths here start there.
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

COPY opencode-agent/firewall.sh opencode-agent/entrypoint.sh opencode-agent/start-agent.sh /usr/local/bin/
COPY opencode-agent/opencode.json /etc/opencode/opencode.json
# OpenCode adds the global AGENTS.md to every session's instructions, next to
# the working repo's own. It tells the agent about this system.
COPY opencode-agent/system-prompt.md /home/node/.config/opencode/AGENTS.md

# Normalise line endings (files may be edited on Windows), lock down the
# root-owned scripts and config, and pre-create volume mount points owned by
# the unprivileged user so fresh named volumes inherit that ownership.
RUN sed -i 's/\r$//' /usr/local/bin/*.sh /etc/opencode/opencode.json \
 && chmod 755 /usr/local/bin/*.sh \
 && chmod 644 /etc/opencode/opencode.json /home/node/.config/opencode/AGENTS.md \
 && mkdir -p /work /home/node/.local/share/opencode /home/node/.config /home/node/.cache \
 && chown -R node:node /work /home/node

# A read-only copy of opencord itself, for questions about the system. Last,
# since it changes with every commit. The root .dockerignore keeps secrets/
# out, and the build fails if they ever get in.
ARG REVISION=unknown
COPY . /opt/opencord/
RUN if [ -e /opt/opencord/secrets ]; then echo "secrets/ is in the build context; refusing to build" >&2; exit 1; fi \
 && echo "$REVISION" > /opt/opencord/REVISION \
 && chown -R root:root /opt/opencord \
 && chmod -R u=rwX,go=rX /opt/opencord

ENV OPENCODE_CONFIG=/etc/opencode/opencode.json
WORKDIR /work
EXPOSE 4096

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
  CMD curl -fsS -u "opencode:${OPENCODE_SERVER_PASSWORD}" http://127.0.0.1:4096/global/health >/dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
