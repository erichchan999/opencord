#!/bin/sh
# Runs as root: install the egress firewall, then drop to the unprivileged
# "node" user with every capability removed before anything user-driven runs.
set -eu

/usr/local/bin/firewall.sh

exec setpriv \
  --reuid=node --regid=node --init-groups \
  --inh-caps=-all --bounding-set=-all \
  --no-new-privs \
  env HOME=/home/node USER=node \
  /usr/local/bin/start-agent.sh
