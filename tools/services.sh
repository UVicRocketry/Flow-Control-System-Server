#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

REMOTE_USER="${REMOTE_USER:-uvr}"
REMOTE_HOST="${REMOTE_HOST:-192.168.0.1}"
REMOTE_PORT="${REMOTE_PORT:-22}"
REMOTE_TARGET="${REMOTE_USER}@${REMOTE_HOST}"
SERVICE_NAME="${SERVICE_NAME:-flow-control-server.service}"
JOURNAL_LINES="${JOURNAL_LINES:-50}"

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

SSH_PASSWORD="$(tr -d '\r\n' < "${PASSWORD_FILE}")"
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

cat > "${ASKPASS_SCRIPT}" <<EOF
#!/usr/bin/env bash
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

run_remote_sudo() {
    local escaped_pw
    escaped_pw="$(printf '%q' "${SSH_PASSWORD}")"
    run_ssh "printf '%s\n' ${escaped_pw} | sudo -S -p '' $*"
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [COMMAND]

Manage '${SERVICE_NAME}' on ${REMOTE_TARGET}.
If no command is given, an interactive menu is shown.

Commands:
  status            Show systemd service status and recent journald logs
  restart           Restart the service and show status
  start             Start the service
  stop              Stop the service
  logs [-f]         Show recent journald logs (pass -f to follow live)
  enable            Enable the service to autostart at boot
  disable           Disable the service from autostarting at boot
  help              Show this help message
EOF
}

do_status() {
    echo "==> Service status for '${SERVICE_NAME}' on ${REMOTE_TARGET}:"
    run_remote_sudo "systemctl --no-pager --full status '${SERVICE_NAME}'" || true
    echo ""
    echo "==> Recent journald logs (last ${JOURNAL_LINES} lines):"
    run_remote_sudo "journalctl -u '${SERVICE_NAME}' -n ${JOURNAL_LINES} --no-pager"
}

do_restart() {
    echo "==> Restarting '${SERVICE_NAME}' on ${REMOTE_TARGET} ..."
    run_remote_sudo "systemctl restart '${SERVICE_NAME}'"
    run_remote_sudo "systemctl --no-pager --full status '${SERVICE_NAME}'" || true
}

do_start() {
    echo "==> Starting '${SERVICE_NAME}' on ${REMOTE_TARGET} ..."
    run_remote_sudo "systemctl start '${SERVICE_NAME}'"
    run_remote_sudo "systemctl --no-pager --full status '${SERVICE_NAME}'" || true
}

do_stop() {
    echo "==> Stopping '${SERVICE_NAME}' on ${REMOTE_TARGET} ..."
    run_remote_sudo "systemctl stop '${SERVICE_NAME}'"
    run_remote_sudo "systemctl --no-pager --full status '${SERVICE_NAME}'" || true
}

do_logs() {
    local follow_flag="${1:-}"
    if [[ "${follow_flag}" == "-f" || "${follow_flag}" == "--follow" ]]; then
        echo "==> Following live journald logs for '${SERVICE_NAME}' on ${REMOTE_TARGET} (Ctrl+C to exit) ..."
        run_remote_sudo "journalctl -u '${SERVICE_NAME}' -n ${JOURNAL_LINES} -f"
    else
        echo "==> Journald logs for '${SERVICE_NAME}' on ${REMOTE_TARGET} (last ${JOURNAL_LINES} lines):"
        run_remote_sudo "journalctl -u '${SERVICE_NAME}' -n ${JOURNAL_LINES} --no-pager"
    fi
}

do_enable() {
    echo "==> Enabling '${SERVICE_NAME}' at boot on ${REMOTE_TARGET} ..."
    run_remote_sudo "systemctl enable '${SERVICE_NAME}'"
    run_remote_sudo "systemctl is-enabled '${SERVICE_NAME}'" || true
}

do_disable() {
    echo "==> Disabling '${SERVICE_NAME}' at boot on ${REMOTE_TARGET} ..."
    run_remote_sudo "systemctl disable '${SERVICE_NAME}'"
    run_remote_sudo "systemctl is-enabled '${SERVICE_NAME}'" || true
}

interactive_menu() {
    echo "=========================================="
    echo " Service Manager: ${SERVICE_NAME} (${REMOTE_TARGET})"
    echo "=========================================="
    echo "1) Check status & journald logs"
    echo "2) Follow live journald logs (journalctl -f)"
    echo "3) Restart service"
    echo "4) Enable service (autostart at boot)"
    echo "5) Disable service"
    echo "6) Start service"
    echo "7) Stop service"
    echo "q) Quit"
    echo "------------------------------------------"
    read -r -p "Select an option [1-7, q]: " choice
    case "${choice}" in
        1) do_status ;;
        2) do_logs "-f" ;;
        3) do_restart ;;
        4) do_enable ;;
        5) do_disable ;;
        6) do_start ;;
        7) do_stop ;;
        q|Q) exit 0 ;;
        *)
            echo "Invalid selection: ${choice}" >&2
            exit 1
            ;;
    esac
}

COMMAND="${1:-}"
if [[ -z "${COMMAND}" ]]; then
    interactive_menu
    exit 0
fi

shift || true
case "${COMMAND}" in
    status)
        do_status
        ;;
    restart)
        do_restart
        ;;
    start)
        do_start
        ;;
    stop)
        do_stop
        ;;
    logs|journal|journald)
        do_logs "${1:-}"
        ;;
    follow|-f)
        do_logs "-f"
        ;;
    enable)
        do_enable
        ;;
    disable)
        do_disable
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        echo "Unknown command: ${COMMAND}" >&2
        usage >&2
        exit 1
        ;;
esac
