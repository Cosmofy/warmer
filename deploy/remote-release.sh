#!/usr/bin/env bash
# Invoked over SSH only by the separately approved manual workflow.
# No bootstrap, unit/Caddy/collector installation, secrets, scheduling or state sync.
set -euo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1

mode=${1:-}
app_port=${2:-}
release_id=${3:-}
[[ "$mode" == preflight || "$mode" == promote ]] || exit 1
[[ "$app_port" =~ ^[1-9][0-9]{3,4}$ ]] || exit 1
(( app_port >= 1024 && app_port <= 65535 )) || exit 1
[[ "$release_id" =~ ^[a-f0-9]{40}-[0-9]+-[0-9]+$ ]] || exit 1

app_path=/home/ubuntu/services/warmer
release_path="$app_path/releases/$release_id"
service=cosmofy-warmer.service
marker=/etc/cosmofy/warmer-bootstrapped

test "$(id -un)" = ubuntu
test -r "$marker"
test ! -w "$marker"
test "$(stat -c %u "$marker")" = 0
test "$(< "$marker")" = single-replica-v1
test "$(realpath -e "$app_path")" = "$app_path"
test "$(realpath -e "$app_path/releases")" = "$app_path/releases"
test -s "$app_path/.env"
test "$(stat -c %a "$app_path/.env")" = 600
grep -Fxq 'WARMER_STATE_DB=/var/lib/cosmofy-warmer/state.db' "$app_path/.env"
test -L "$app_path/current"
previous_release=$(realpath -e "$app_path/current")
[[ "$previous_release" == "$app_path/releases/"* ]] || exit 1
test -x "$previous_release/.venv/bin/uvicorn"
test -x /home/ubuntu/.local/bin/uv
test "$(realpath -e /var/lib/cosmofy-warmer)" = /var/lib/cosmofy-warmer
test -s /var/lib/cosmofy-warmer/state.db
test ! -L /var/lib/cosmofy-warmer/state.db
test -w /var/lib/cosmofy-warmer/state.db
test "$(systemctl show --property=FragmentPath --value "$service")" = "/etc/systemd/system/$service"
test -z "$(systemctl show --property=DropInPaths --value "$service")"
grep -Fq -- "--port $app_port --workers 1" "/etc/systemd/system/$service"
systemctl is-active --quiet "$service"
curl --noproxy '*' -fsS --max-time 5 --output /dev/null \
    "http://127.0.0.1:$app_port/health/ready"

if [[ "$mode" == preflight ]]; then
    test ! -e "$release_path"
    test ! -L "$release_path"
    printf '%s\n' 'Bootstrap preflight passed; no files changed.'
    exit 0
fi

# Host-local serialization supplements the single GitHub deployment group.
exec 9> "$app_path/.deploy.lock"
flock -n 9
test "$(realpath -e "$app_path/current")" = "$previous_release"
test "$(realpath -e "$release_path")" = "$release_path"
test -s "$release_path/app/main.py"
test -s "$release_path/pyproject.toml"
test -s "$release_path/uv.lock"
# Infrastructure changes need their own approved maintenance operation.
cmp --silent "$release_path/deploy/cosmofy-warmer.service" "/etc/systemd/system/$service"
cd "$release_path"
/home/ubuntu/.local/bin/uv sync --locked --no-dev --python 3.14
test -x .venv/bin/uvicorn
test ! -e "$app_path/current.next"
test ! -L "$app_path/current.next"

check_health() {
    local attempt
    for ((attempt = 1; attempt <= 30; attempt++)); do
        if systemctl is-active --quiet "$service" \
            && curl --noproxy '*' -fsS --max-time 3 --output /dev/null \
                "http://127.0.0.1:$app_port/health/live" \
            && curl --noproxy '*' -fsS --max-time 3 --output /dev/null \
                "http://127.0.0.1:$app_port/health/ready"; then
            return 0
        fi
        sleep 2
    done
    return 1
}

promotion_started=false
rollback() {
    local result=$?
    trap - EXIT INT TERM
    if [[ "$promotion_started" == true ]]; then
        printf '%s\n' 'Release failed; restoring previous code only. SQLite is untouched.' >&2
        # Remove only our known staging symlink, never a directory or state file.
        if test -L "$app_path/current.next" \
            && [[ "$(readlink "$app_path/current.next")" == "$release_path" ]]; then
            unlink "$app_path/current.next"
        fi
        # Stop first so two workers cannot access the persistent state concurrently.
        if sudo -n systemctl stop "$service" \
            && ln -s "$previous_release" "$app_path/current.next" \
            && mv -Tf "$app_path/current.next" "$app_path/current" \
            && sudo -n systemctl start "$service" \
            && check_health; then
            printf '%s\n' 'Previous release is healthy; deployment remains failed.' >&2
        else
            printf '%s\n' 'Rollback requires operator recovery. Keep triggers paused; do not restore SQLite blindly.' >&2
        fi
    fi
    exit "$result"
}
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Approval confirms producers are paused and persistent background runs drained.
# No warm or extract endpoint is called by deployment or health verification.
promotion_started=true
sudo -n systemctl stop "$service"
ln -s "$release_path" "$app_path/current.next"
mv -Tf "$app_path/current.next" "$app_path/current"
sudo -n systemctl start "$service"
check_health
promotion_started=false
printf 'Healthy release: %s\nPrevious code retained: %s\n' "$release_id" "$previous_release"
printf '%s\n' 'Triggers remain paused until separate operator verification. No state or release cleanup performed.'
