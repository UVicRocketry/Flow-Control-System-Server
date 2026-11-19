#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

source "${SCRIPT_DIR}/_common.sh"

SERVICE_NAME="${SERVICE_NAME:-flow-control-server.service}"
ENTRY_SCRIPT="${ENTRY_SCRIPT:-${REMOTE_DIR}/contents/code/main.py}"
VENDOR_WHEELS_DIR="${VENDOR_WHEELS_DIR:-${SCRIPT_DIR}/vendor/wheels}"

init_ssh

echo "==> Connecting to ${REMOTE_TARGET} and preparing ${REMOTE_DIR} ..."
run_ssh "mkdir -p '${REMOTE_DIR}'"

RSYNC_EXTRA_OPTS=()
if [[ "${1:-}" == "--force" || "${1:-}" == "-f" ]]; then
    RSYNC_EXTRA_OPTS+=(--ignore-times)
fi

echo "==> Pushing project '${PROJECT_NAME}' to ${REMOTE_TARGET}:${REMOTE_DIR} (incremental sync) ..."
if command -v rsync >/dev/null 2>&1; then
    RSYNC_SSH="ssh ${SSH_OPTS[*]}"
    RSYNC_CMD=(
        rsync -avz --delete
        "${RSYNC_EXTRA_OPTS[@]}"
        --exclude='data/'
        --exclude='.venv/'
        --exclude='tools/vendor/'
        --exclude='password.txt'
        --exclude='**/password.txt'
        --filter=':- .gitignore'
        -e "${RSYNC_SSH}"
        "${REPO_ROOT}/" "${REMOTE_TARGET}:${REMOTE_DIR}/"
    )
    if command -v sshpass >/dev/null 2>&1; then
        sshpass -p "${SSH_PASSWORD}" "${RSYNC_CMD[@]}"
    else
        "${RSYNC_CMD[@]}"
    fi
else
    git -C "${REPO_ROOT}" archive --format=tar HEAD | run_ssh "tar -xf - -C '${REMOTE_DIR}'"
fi

echo "==> Ensuring remote virtual environment '${REMOTE_VENV}' on ${REMOTE_TARGET} ..."
HAS_LOCAL_WHEELS=0
if [[ -d "${VENDOR_WHEELS_DIR}" && -n "$(ls -A "${VENDOR_WHEELS_DIR}" 2>/dev/null)" ]]; then
    HAS_LOCAL_WHEELS=1
fi

if ! run_ssh "test -x '${REMOTE_VENV}/bin/python' && '${REMOTE_VENV}/bin/python' -c 'import websockets, serial, u6' >/dev/null 2>&1"; then
    run_ssh "if [[ ! -x '${REMOTE_VENV}/bin/python' ]]; then python3 -m venv --system-site-packages '${REMOTE_VENV}'; fi"
    if [[ "${HAS_LOCAL_WHEELS}" == "1" ]]; then
        REMOTE_WHEEL_TMP="/tmp/fcs-push-wheels"
        echo "--> Installing dependencies into '${REMOTE_VENV}' from cached local wheelhouse ..."
        run_ssh "rm -rf '${REMOTE_WHEEL_TMP}' && mkdir -p '${REMOTE_WHEEL_TMP}'"
        tar -C "${VENDOR_WHEELS_DIR}" -cf - . | run_ssh "tar -C '${REMOTE_WHEEL_TMP}' -xf -"
        run_ssh "'${REMOTE_VENV}/bin/python' -m pip install --no-index --find-links '${REMOTE_WHEEL_TMP}' -r '${REMOTE_DIR}/requirements.txt' && rm -rf '${REMOTE_WHEEL_TMP}'" || true
    elif [[ -f "${REPO_ROOT}/requirements.txt" ]]; then
        echo "--> Attempting .venv dependency sync (skips gracefully if offline) ..."
        if ! run_ssh "'${REMOTE_VENV}/bin/python' -m pip install --timeout 5 --retries 1 -r '${REMOTE_DIR}/requirements.txt'"; then
            echo "Warning: Could not install dependencies into '${REMOTE_VENV}' (offline at test pad?). Run './tools/services.sh fetch-deps && ./tools/services.sh push-deps' while connected." >&2
        fi
    fi
fi

echo "==> Configuring and enabling systemd service '${SERVICE_NAME}' on ${REMOTE_TARGET} ..."
ESCAPED_PW="$(printf '%q' "${SSH_PASSWORD}")"
run_ssh "bash -s" <<EOF
set -euo pipefail

UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}"

sudo_cmd() {
    printf '%s\n' ${ESCAPED_PW} | sudo -S -p '' "\$@"
}

sudo_cmd cp "${REMOTE_DIR}/contents/config/${SERVICE_NAME}" "\${UNIT_FILE}"
sudo_cmd chmod 644 "\${UNIT_FILE}"
sudo_cmd systemctl daemon-reload
sudo_cmd systemctl enable "${SERVICE_NAME}"
sudo_cmd systemctl restart "${SERVICE_NAME}"
sudo_cmd systemctl --no-pager --full status "${SERVICE_NAME}" || true
EOF

echo "==> Deployment complete! '${SERVICE_NAME}' is enabled and running on ${REMOTE_TARGET}."
