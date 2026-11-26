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

if [[ "${SKIP_DEPS_CHECK:-0}" == "1" ]]; then
    echo "==> Skipping remote dependency check (SKIP_DEPS_CHECK=1)."
elif ! run_ssh "test -x '${REMOTE_VENV}/bin/python' && '${REMOTE_VENV}/bin/python' -c 'import fastapi, uvicorn, pydantic, websockets, serial, u6' >/dev/null 2>&1"; then
    run_ssh "if [[ ! -x '${REMOTE_VENV}/bin/python' ]] || [[ ! -f '${REMOTE_VENV}/bin/activate' ]] || grep -q 'include-system-site-packages = false' '${REMOTE_VENV}/pyvenv.cfg' 2>/dev/null; then rm -rf '${REMOTE_VENV}'; python3 -m venv --without-pip --system-site-packages '${REMOTE_VENV}' || python3 -m venv --system-site-packages '${REMOTE_VENV}'; fi"
    if [[ "${HAS_LOCAL_WHEELS}" == "1" ]]; then
        REMOTE_WHEEL_TMP="/tmp/fcs-push-wheels"
        echo "--> Installing dependencies into '${REMOTE_VENV}' from cached local wheelhouse ..."
        run_ssh "rm -rf '${REMOTE_WHEEL_TMP}' && mkdir -p '${REMOTE_WHEEL_TMP}'"
        tar -C "${VENDOR_WHEELS_DIR}" -cf - . | run_ssh "tar -C '${REMOTE_WHEEL_TMP}' -xf -"
        if ! run_ssh "bash -s" <<EOF
set -euo pipefail
trap 'rm -rf "${REMOTE_WHEEL_TMP}"' EXIT
echo "--> Target Python: \$("${REMOTE_VENV}/bin/python" --version) (\$(uname -m))"
PIP_WHL="\$(ls -1 "${REMOTE_WHEEL_TMP}"/pip-*.whl 2>/dev/null | sort -V | tail -n 1 || true)"
if [[ -n "\${PIP_WHL}" ]]; then
    PYTHONPATH="\${PIP_WHL}" "${REMOTE_VENV}/bin/python" -m pip install --no-index --find-links "${REMOTE_WHEEL_TMP}" --upgrade pip
fi
"${REMOTE_VENV}/bin/python" -m pip install --no-index --find-links "${REMOTE_WHEEL_TMP}" -r "${REMOTE_DIR}/requirements.txt"
EOF
        then
            echo "Error: Offline wheel installation failed into '${REMOTE_VENV}'." >&2
            echo "Fetch wheels matching the cart's Python version with: TARGET_PY_VERSIONS=<ver> ./tools/services.sh fetch-deps" >&2
            exit 1
        fi
    elif [[ -f "${REPO_ROOT}/requirements.txt" ]]; then
        if [[ "${ALLOW_ONLINE_PIP:-0}" == "1" ]]; then
            echo "--> Attempting online .venv dependency sync (ALLOW_ONLINE_PIP=1) ..."
            if ! run_ssh "'${REMOTE_VENV}/bin/python' -m pip install --timeout 5 --retries 1 -r '${REMOTE_DIR}/requirements.txt'"; then
                echo "Error: Online pip install failed. Run './tools/services.sh fetch-deps' while connected and re-run './tools/push-code.sh'." >&2
                exit 1
            fi
        else
            echo "Error: Required Python packages are missing in '${REMOTE_VENV}' on ${REMOTE_TARGET}, and '${VENDOR_WHEELS_DIR}' is empty." >&2
            echo "Because the server runs offline, push-code.sh will not attempt an internet install by default." >&2
            echo "Run './tools/services.sh fetch-deps' while online, then re-run './tools/push-code.sh' (or './tools/services.sh push-deps')." >&2
            echo "Overrides: ALLOW_ONLINE_PIP=1 (try PyPI) or SKIP_DEPS_CHECK=1 (deploy code only)." >&2
            exit 1
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
