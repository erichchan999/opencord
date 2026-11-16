#!/bin/sh
# Runs as the unprivileged "node" user with no capabilities.
# Clones the repo on first start, configures git + gh, then starts OpenCode.
set -eu

: "${REPO:?Set REPO=owner/name in secrets/agent.env}"

git config --global user.name "${GIT_AUTHOR_NAME:-fx-sandbox}"
git config --global user.email "${GIT_AUTHOR_EMAIL:-fx-sandbox@users.noreply.github.com}"
git config --global init.defaultBranch main

case "${GH_TOKEN:-}" in
  ""|replace-me*)
    echo "start-agent: GH_TOKEN is not set; cloning a private repo, pushing and PRs will not work" >&2
    unset GH_TOKEN
    ;;
  *)
    # Use the scoped token for git over HTTPS as well as for gh.
    gh auth setup-git
    ;;
esac

if [ ! -e /work/repo/.git ]; then
  echo "start-agent: cloning $REPO into /work/repo"
  git clone "https://github.com/$REPO.git" /work/repo
fi

cd /work/repo
echo "start-agent: starting opencode $(opencode --version 2>/dev/null || true)"
exec opencode serve --hostname 0.0.0.0 --port 4096
