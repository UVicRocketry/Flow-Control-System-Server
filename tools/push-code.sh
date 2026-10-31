#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

REMOTE_USER="${REMOTE_USER:-uvr}"
REMOTE_HOST="${REMOTE_HOST:-192.168.0.1}"
REMOTE_PORT="${REMOTE_PORT:-22}"
REMOTE_TARGET="${REMOTE_USER}@${REMOTE_HOST}"
PROJECT_NAME="Flow-Control-System-Server"
REMOTE_DIR="${REMOTE_DIR:-/home/${REMOTE_USER}/${PROJECT_NAME}}"
SERVICE_NAME="${SERVICE_NAME:-flow-control-server.service}"
ENTRY_SCRIPT="${ENTRY_SCRIPT:-${REMOTE_DIR}/contents/code/main.py}"

# Locate the gitignored password file (checks tools/password.txt then repo-root password.txt)
if [[ -z "${PASSWORD_FILE:-}" ]]; then
    if [[ -f "${SCRIPT_DIR}/password.txt" ]]; then
        PASSWORD_FILE="${SCRIPT_DIR}/password.txt"
    elif [[ -f "${REPO_ROOT}/password.txt" ]]; then
        PASSWORD_FILE="${REPO_ROOT}/password.txt"
    else
        PASSWORD_FILE="${SCRIPT_DIR}/password.txt"
    fi
fi

if [[ ! -f "${PASSWORD_FILE}" ]]; then
    echo "Error: Password file not found at '${PASSWORD_FILE}'." >&2
    echo "Create '${SCRIPT_DIR}/password.txt' (or '${REPO_ROOT}/password.txt') with the SSH/sudo password for ${REMOTE_TARGET}." >&2
    exit 1
fi

SSH_PASSWORD="$(tr -d '\r\n' <"${PASSWORD_FILE}")"
if [[ -z "${SSH_PASSWORD}" ]]; then
    echo "Error: Password file '${PASSWORD_FILE}' is empty." >&2
    exit 1
fi

# Create a temporary SSH_ASKPASS helper and ControlMaster socket directory
ASKPASS_SCRIPT="$(mktemp)"
CTL_DIR="$(mktemp -d)"
cleanup() {
    ssh -O exit -o ControlPath="${CTL_DIR}/mux" "${REMOTE_TARGET}" 2>/dev/null || true
    rm -rf "${ASKPASS_SCRIPT}" "${CTL_DIR}"
}
trap cleanup EXIT

cat >"${ASKPASS_SCRIPT}" <<EOF
#!/usr/bin/env bash
# If OpenSSH asks a host-key yes/no question via SSH_ASKPASS, answer yes; otherwise output password
if [[ "\${1:-}" == *"yes/no"* ]]; then
    printf 'yes\n'
else
    printf '%s\n' $(printf '%q' "${SSH_PASSWORD}")
fi
EOF
chmod 700 "${ASKPASS_SCRIPT}"

export SSH_ASKPASS="${ASKPASS_SCRIPT}"
export SSH_ASKPASS_REQUIRE=force
export DISPLAY="${DISPLAY:-:0}"

SSH_OPTS=(
    -4
    -p "${REMOTE_PORT}"
    -o StrictHostKeyChecking=accept-new
    -o GSSAPIAuthentication=no
    -o PreferredAuthentications=password,keyboard-interactive
    -o PubkeyAuthentication=no
    -o ControlMaster=auto
    -o ControlPath="${CTL_DIR}/mux"
    -o ControlPersist=60
)

if [[ "${SSH_VERBOSE:-0}" == "1" ]]; then
    SSH_OPTS+=(-v)
fi

run_ssh() {
    if command -v sshpass >/dev/null 2>&1; then
        sshpass -p "${SSH_PASSWORD}" ssh "${SSH_OPTS[@]}" "${REMOTE_TARGET}" "$@"
    else
        ssh "${SSH_OPTS[@]}" "${REMOTE_TARGET}" "$@"
    fi
}

echo "==> Connecting to ${REMOTE_TARGET} and preparing ${REMOTE_DIR} ..."
run_ssh "mkdir -p '${REMOTE_DIR}'"

RSYNC_EXTRA_OPTS=()
if [[ "${1:-}" == "--force" || "${1:-}" == "-f" ]]; then
    RSYNC_EXTRA_OPTS+=(--ignore-times)
fi

echo "==> Pushing project '${PROJECT_NAME}' to ${REMOTE_TARGET}:${REMOTE_DIR} (incremental sync) ..."
if command -v rsync >/dev/null 2>&1; then
    RSYNC_SSH="ssh ${SSH_OPTS[*]}"
    if command -v sshpass >/dev/null 2>&1; then
        sshpass -p "${SSH_PASSWORD}" rsync -avz --delete \
            "${RSYNC_EXTRA_OPTS[@]}" \
            --exclude='password.txt' \
            --exclude='**/password.txt' \
            --filter=':- .gitignore' \
            -e "${RSYNC_SSH}" \
            "${REPO_ROOT}/" "${REMOTE_TARGET}:${REMOTE_DIR}/"
    else
        rsync -avz --delete \
            "${RSYNC_EXTRA_OPTS[@]}" \
            --exclude='password.txt' \
            --exclude='**/password.txt' \
            --filter=':- .gitignore' \
            -e "${RSYNC_SSH}" \
            "${REPO_ROOT}/" "${REMOTE_TARGET}:${REMOTE_DIR}/"
    fi
else
    git -C "${REPO_ROOT}" archive --format=tar HEAD | run_ssh "tar -xf - -C '${REMOTE_DIR}'"
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
