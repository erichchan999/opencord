#!/bin/sh
# Runs as the unprivileged "node" user with no capabilities.
# Sets up git and the sandbox folder, then starts OpenCode.
set -eu

# Defaults for any repo the agent clones or creates in the sandbox.
git config --global user.name "${GIT_AUTHOR_NAME:-opencord}"
git config --global user.email "${GIT_AUTHOR_EMAIL:-agent@opencord.invalid}"
git config --global init.defaultBranch main

# Every thread works here. It starts empty, is not a git repo and is kept across restarts.
mkdir -p /work/sandbox
cd /work/sandbox
echo "start-agent: starting opencode $(opencode --version 2>/dev/null || true)"
exec opencode serve --hostname 0.0.0.0 --port 4096
