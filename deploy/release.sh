#!/usr/bin/env bash
# GitHub manual-workflow entry point. DO NOT run during preparation/bootstrap.
set -euo pipefail
umask 077

[[ "${GITHUB_EVENT_NAME:-}" == workflow_dispatch ]] || exit 1
[[ "${GITHUB_REF:-}" == refs/heads/main ]] || exit 1
[[ "${WARMER_DEPLOY_ENABLED:-}" == true ]] || exit 1
[[ "${WARMER_BOOTSTRAPPED:-}" == true ]] || exit 1
[[ "${DEPLOY_HOST:-}" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || exit 1
[[ "${APP_PORT:-}" =~ ^[1-9][0-9]{3,4}$ ]] || exit 1
(( APP_PORT >= 1024 && APP_PORT <= 65535 )) || exit 1
[[ "${GITHUB_SHA:-}" =~ ^[a-f0-9]{40}$ ]] || exit 1
[[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ ]] || exit 1
[[ "${GITHUB_RUN_ATTEMPT:-}" =~ ^[0-9]+$ ]] || exit 1
[[ "$(git rev-parse HEAD)" == "$GITHUB_SHA" ]] || exit 1
test -n "${DEPLOY_SSH_KEY:-}"
test -n "${DEPLOY_KNOWN_HOSTS:-}"
test -s pyproject.toml
test -s uv.lock
test -s app/main.py
# The variable must agree with the reviewed unit; it does not allocate a port.
grep -Fq -- "--port $APP_PORT --workers 1" deploy/cosmofy-warmer.service

app_path=/home/ubuntu/services/warmer
release_id="$GITHUB_SHA-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
release_path="$app_path/releases/$release_id"
ssh_dir=$(mktemp -d "${RUNNER_TEMP:?}/warmer-ssh.XXXXXX")
cleanup() {
    rm -f -- "$ssh_dir/key" "$ssh_dir/known_hosts"
    rmdir -- "$ssh_dir"
}
trap cleanup EXIT
printf '%s\n' "$DEPLOY_SSH_KEY" > "$ssh_dir/key"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$ssh_dir/known_hosts"
unset DEPLOY_SSH_KEY DEPLOY_KNOWN_HOSTS
ssh_args=(-i "$ssh_dir/key" -o "UserKnownHostsFile=$ssh_dir/known_hosts"
    -o StrictHostKeyChecking=yes -o BatchMode=yes -o IdentitiesOnly=yes
    -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)

# Read-only remote gate FIRST: no mkdir, upload, install or restart before this.
# Deliberate client expansion of the regex-validated port and release identifier.
# shellcheck disable=SC2029
ssh "${ssh_args[@]}" "ubuntu@$DEPLOY_HOST" \
    "bash -s -- preflight '$APP_PORT' '$release_id'" < deploy/remote-release.sh

# A fresh release only. Never rsync to current/, .env or /var/lib, never --delete.
# The path combines a fixed base and the validated release identifier.
# shellcheck disable=SC2029
ssh "${ssh_args[@]}" "ubuntu@$DEPLOY_HOST" "mkdir -m 700 '$release_path'"
export RSYNC_RSH="ssh -i '$ssh_dir/key' -o 'UserKnownHostsFile=$ssh_dir/known_hosts' -o StrictHostKeyChecking=yes -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=10"
rsync -az \
    --exclude '.env*' \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '*.db*' \
    --exclude '*.sqlite*' \
    --exclude 'state' \
    --exclude 'data' \
    app deploy pyproject.toml uv.lock "ubuntu@$DEPLOY_HOST:$release_path/"
if test -f .python-version; then
    rsync -az .python-version "ubuntu@$DEPLOY_HOST:$release_path/"
fi

# Deliberate client expansion of the same validated arguments as preflight.
# shellcheck disable=SC2029
ssh "${ssh_args[@]}" "ubuntu@$DEPLOY_HOST" \
    "bash -s -- promote '$APP_PORT' '$release_id'" < deploy/remote-release.sh
