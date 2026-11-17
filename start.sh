#!/bin/sh
# opencord - build, deploy and manage the containers on macOS and Linux.
#
#   ./start.sh            build and (re)start everything from this folder
#   ./start.sh --status   show container status and which commit each one runs
#   ./start.sh --logs     follow the logs (Ctrl+C to stop following)
#   ./start.sh --stop     stop the containers (volumes and the repo clone are kept)
#
# start.ps1 does the same on Windows. Docker Desktop's dashboard shows the
# same containers under the "opencord" project, with an "opencord.revision"
# label naming the commit.
set -eu

here="$(cd "$(dirname "$0")" && pwd)"

compose() {
    docker compose --project-directory "$here" -f "$here/compose.yaml" "$@"
}

# The commit this folder is at, with "-dirty" if it has uncommitted changes.
revision() {
    rev="$(git -C "$here" rev-parse --short HEAD 2>/dev/null)" || rev=""
    if [ -z "$rev" ]; then
        echo unknown
        return
    fi
    if [ -n "$(git -C "$here" status --porcelain 2>/dev/null)" ]; then
        rev="$rev-dirty"
    fi
    echo "$rev"
}

if ! docker info >/dev/null 2>&1; then
    echo "Docker is not running. Start it and try again." >&2
    exit 1
fi

case "${1:-}" in
    --status)
        ids="$(compose ps -a -q)"
        if [ -z "$ids" ]; then
            echo "No containers. Run ./start.sh to deploy."
            exit 0
        fi
        local_rev="$(revision)"
        printf '%-16s %-20s %-17s %-14s %s\n' SERVICE STATE "SINCE (UTC)" REVISION UP-TO-DATE
        # shellcheck disable=SC2086  # one argument per container id
        docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}|{{.State.Status}}{{if .State.Health}} ({{.State.Health.Status}}){{end}}|{{.State.StartedAt}}|{{index .Config.Labels "opencord.revision"}}' $ids |
            sort |
            while IFS='|' read -r service state since deployed; do
                current=no
                case "$local_rev" in
                    *-dirty | unknown) ;;
                    *) [ "$deployed" = "$local_rev" ] && current=yes ;;
                esac
                since="$(echo "$since" | cut -c1-16 | tr T ' ')"
                printf '%-16s %-20s %-17s %-14s %s\n' "$service" "$state" "$since" "$deployed" "$current"
            done
        echo "Local folder is at $local_rev."
        exit 0
        ;;
    --logs)
        compose logs -f --tail 100
        exit 0
        ;;
    --stop)
        compose stop
        exit 0
        ;;
    "") ;;
    *)
        echo "usage: ./start.sh [--status | --logs | --stop]" >&2
        exit 2
        ;;
esac

# Check secrets before deploying.
check() {
    file="$1"
    shift
    if [ ! -f "$here/$file" ]; then
        echo "Missing $file. Copy it from secrets.example and fill it in." >&2
        exit 1
    fi
    for key in "$@"; do
        if ! grep -Eq "^$key=[^[:space:]]" "$here/$file" || grep -Eq "^$key=replace-me" "$here/$file"; then
            echo "WARNING: $file: $key is not set yet. That part will not work until you fill it in." >&2
        fi
    done
}
check secrets/proxy.env DEEPSEEK_API_KEY
check secrets/agent.env REPO GH_TOKEN
check secrets/bridge.env DISCORD_BOT_TOKEN
check secrets/shared.env OPENCODE_SERVER_PASSWORD

# compose.yaml puts this on every container as the "opencord.revision" label. A new
# value recreates the containers, so the label always names what is running.
OPENCORD_REVISION="$(revision)"
export OPENCORD_REVISION
echo "Building and starting containers at $OPENCORD_REVISION ..."
compose up -d --build --remove-orphans
compose ps
