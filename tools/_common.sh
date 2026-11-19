#!/usr/bin/env bash
# Shared SSH, sudo, reachability, and remote venv helpers for tools/*.sh scripts.

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="${SCRIPT_DIR:-${COMMON_DIR}}"
REPO_ROOT="${REPO_ROOT:-$(cd "${COMMON_DIR}/.." && pwd)}"

REMOTE_USER="${REMOTE_USER:-uvr}"
REMOTE_HOST="${REMOTE_HOST:-192.168.0.1}"
REMOTE_PORT="${REMOTE_PORT:-22}"
REMOTE_TARGET="${REMOTE_TARGET:-${REMOTE_USER}@${REMOTE_HOST}}"
PROJECT_NAME="${PROJECT_NAME:-Flow-Control-System-Server}"
REMOTE_DIR="${REMOTE_DIR:-/home/${REMOTE_USER}/${PROJECT_NAME}}"
REMOTE_VENV="${REMOTE_VENV:-${REMOTE_DIR}/.venv}"

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

SSH_PASSWORD=""
ASKPASS_SCRIPT=""
CTL_DIR=""
SSH_OPTS=()

cleanup_ssh() {
    if [[ -n "${CTL_DIR}" && -S "${CTL_DIR}/mux" ]]; then
        ssh -O exit -o ControlPath="${CTL_DIR}/mux" "${REMOTE_TARGET}" 2>/dev/null || true
    fi
    rm -rf "${ASKPASS_SCRIPT}" "${CTL_DIR}" 2>/dev/null || true
}
trap cleanup_ssh EXIT

init_ssh() {
    if [[ -n "${ASKPASS_SCRIPT}" && -n "${CTL_DIR}" && -n "${SSH_PASSWORD}" && ${#SSH_OPTS[@]} -gt 0 ]]; then
        return 0
    fi

    if [[ ! -f "${PASSWORD_FILE}" ]]; then
        echo "Error: Password file not found at '${PASSWORD_FILE}'." >&2
        echo "Create '${SCRIPT_DIR}/password.txt' (or '${REPO_ROOT}/password.txt') with the SSH/sudo password for ${REMOTE_TARGET}." >&2
        return 1
    fi

    SSH_PASSWORD="$(tr -d '\r\n' <"${PASSWORD_FILE}")"
    if [[ -z "${SSH_PASSWORD}" ]]; then
        echo "Error: Password file '${PASSWORD_FILE}' is empty." >&2
        return 1
    fi

    if [[ -z "${ASKPASS_SCRIPT}" ]]; then
        ASKPASS_SCRIPT="$(mktemp)"
        CTL_DIR="$(mktemp -d)"
        cat >"${ASKPASS_SCRIPT}" <<EOF
#!/usr/bin/env bash
# Answer host-key prompts with yes; otherwise output the configured SSH password.
if [[ "\${1:-}" == *"yes/no"* ]]; then
    printf 'yes\n'
else
    printf '%s\n' $(printf '%q' "${SSH_PASSWORD}")
fi
EOF
        chmod 700 "${ASKPASS_SCRIPT}"
    fi

    export SSH_ASKPASS="${ASKPASS_SCRIPT}"
    export SSH_ASKPASS_REQUIRE=force
    export DISPLAY="${DISPLAY:-:0}"

    SSH_OPTS=(
        -4
        -o "Port=${REMOTE_PORT}"
        -o StrictHostKeyChecking=accept-new
        -o GSSAPIAuthentication=no
        -o PreferredAuthentications=password,keyboard-interactive
        -o PubkeyAuthentication=no
        -o ConnectTimeout=5
        -o ControlMaster=auto
        -o ControlPath="${CTL_DIR}/mux"
        -o ControlPersist=60
    )
    if [[ "${SSH_VERBOSE:-0}" == "1" ]]; then
        SSH_OPTS+=(-v)
    fi
}

ssh_opts() {
    init_ssh || return 1
    printf '%s\n' "${SSH_OPTS[@]}"
}

run_ssh() {
    init_ssh || return 1
    if command -v sshpass >/dev/null 2>&1; then
        sshpass -p "${SSH_PASSWORD}" ssh "${SSH_OPTS[@]}" "${REMOTE_TARGET}" "$@"
    else
        ssh "${SSH_OPTS[@]}" "${REMOTE_TARGET}" "$@"
    fi
}

run_scp_to() {
    local local_path="$1"
    local remote_path="$2"
    init_ssh || return 1
    if command -v sshpass >/dev/null 2>&1; then
        sshpass -p "${SSH_PASSWORD}" scp "${SSH_OPTS[@]}" "${local_path}" "${REMOTE_TARGET}:${remote_path}"
    else
        scp "${SSH_OPTS[@]}" "${local_path}" "${REMOTE_TARGET}:${remote_path}"
    fi
}

run_remote_sudo() {
    init_ssh || return 1
    local escaped_pw
    escaped_pw="$(printf '%q' "${SSH_PASSWORD}")"
    run_ssh "printf '%s\n' ${escaped_pw} | sudo -S -p '' $*"
}

check_tcp_port() {
    local host="$1"
    local port="$2"
    if command -v nc >/dev/null 2>&1; then
        nc -z -w 2 "${host}" "${port}" >/dev/null 2>&1
    else
        timeout 2 bash -c "exec 3<>/dev/tcp/${host}/${port}" 2>/dev/null
    fi
}

is_valve_cart_reachable() {
    check_tcp_port "${REMOTE_HOST}" "${REMOTE_PORT}" && run_ssh "true" >/dev/null 2>&1
}

require_connection() {
    if ! is_valve_cart_reachable; then
        echo "Error: Cannot connect to valve cart at ${REMOTE_TARGET} (port ${REMOTE_PORT})." >&2
        echo "Ensure the valve cart is powered on and reachable (or run tools/direct-ethernet.sh if connected directly)." >&2
        return 1
    fi
}
