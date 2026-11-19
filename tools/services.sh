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
    -o ConnectTimeout=5
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
  fetch-deps        Download Python wheels & LabJack exodriver into tools/vendor/
  push-deps         Push cached dependencies over SSH & install offline on valve cart
  direct-eth [args] Open or forward commands to tools/direct-ethernet.sh
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

VENDOR_DIR="${SCRIPT_DIR}/vendor"
VENDOR_WHEELS_DIR="${VENDOR_DIR}/wheels"
VENDOR_EXODRIVER_DIR="${VENDOR_DIR}/exodriver"
REQUIREMENTS_FILE="${REPO_ROOT}/requirements.txt"

do_fetch_deps() {
    echo "==> Preparing local offline dependency bundle in '${VENDOR_DIR}' ..."
    mkdir -p "${VENDOR_WHEELS_DIR}"

    if [[ ! -f "${REQUIREMENTS_FILE}" ]]; then
        echo "Error: Requirements file not found at '${REQUIREMENTS_FILE}'." >&2
        return 1
    fi

    echo "==> Downloading Python packages from '${REQUIREMENTS_FILE}' ..."
    # Download universal wheels / source distributions so they work across Python versions on the cart
    python3 -m pip download \
        --dest "${VENDOR_WHEELS_DIR}" \
        -r "${REQUIREMENTS_FILE}"
    python3 -m pip download \
        --no-binary :all: \
        --dest "${VENDOR_WHEELS_DIR}" \
        -r "${REQUIREMENTS_FILE}" || true

    if [[ ! -d "${VENDOR_EXODRIVER_DIR}/liblabjackusb" ]]; then
        if command -v git >/dev/null 2>&1; then
            echo "==> Cloning LabJack exodriver into '${VENDOR_EXODRIVER_DIR}' ..."
            rm -rf "${VENDOR_EXODRIVER_DIR}"
            git clone --depth 1 https://github.com/labjack/exodriver.git "${VENDOR_EXODRIVER_DIR}" || \
                echo "Warning: Could not clone LabJack exodriver (check internet connection)." >&2
        fi
    else
        echo "==> LabJack exodriver already cached at '${VENDOR_EXODRIVER_DIR}'."
    fi

    echo "==> Offline dependency bundle ready in '${VENDOR_DIR}':"
    ls -lh "${VENDOR_WHEELS_DIR}"
}

do_push_deps() {
    mkdir -p "${VENDOR_WHEELS_DIR}"
    if [[ -z "$(ls -A "${VENDOR_WHEELS_DIR}" 2>/dev/null)" ]]; then
        echo "==> No cached wheels found in '${VENDOR_WHEELS_DIR}'; attempting local fetch first ..."
        do_fetch_deps
    fi

    if [[ -z "$(ls -A "${VENDOR_WHEELS_DIR}" 2>/dev/null)" ]]; then
        echo "Error: '${VENDOR_WHEELS_DIR}' is empty. Run '$(basename "$0") fetch-deps' while connected to the internet first." >&2
        return 1
    fi

    local remote_tmp="/tmp/fcs-offline-deps"
    echo "==> Uploading offline dependency bundle to ${REMOTE_TARGET}:${remote_tmp} over SSH ..."
    run_ssh "rm -rf '${remote_tmp}' && mkdir -p '${remote_tmp}/wheels'"

    tar -C "${VENDOR_DIR}" -cf - . | run_ssh "tar -C '${remote_tmp}' -xf -"
    if [[ -f "${REQUIREMENTS_FILE}" ]]; then
        cat "${REQUIREMENTS_FILE}" | run_ssh "cat > '${remote_tmp}/requirements.txt'"
    fi

    echo "==> Installing Python dependencies & LabJack permissions on ${REMOTE_TARGET} (offline) ..."
    local escaped_pw
    escaped_pw="$(printf '%q' "${SSH_PASSWORD}")"
    run_ssh "bash -s" <<EOF
set -euo pipefail

sudo_cmd() {
    printf '%s\n' ${escaped_pw} | sudo -S -p '' "\$@"
}

PIP_EXTRA_ARGS=()
if python3 -m pip install --help 2>&1 | grep -q -- '--break-system-packages'; then
    PIP_EXTRA_ARGS+=(--break-system-packages)
fi

echo "--> Installing Python packages for user '${REMOTE_USER}' and system service ..."
python3 -m pip install --user --no-index --find-links "${remote_tmp}/wheels" "\${PIP_EXTRA_ARGS[@]}" -r "${remote_tmp}/requirements.txt" || \
    sudo_cmd python3 -m pip install --no-index --find-links "${remote_tmp}/wheels" "\${PIP_EXTRA_ARGS[@]}" -r "${remote_tmp}/requirements.txt"

if [[ -d "${remote_tmp}/exodriver/liblabjackusb" ]]; then
    if ! ldconfig -p 2>/dev/null | grep -q liblabjackusb; then
        echo "--> Building and installing LabJack exodriver (liblabjackusb) ..."
        if command -v make >/dev/null 2>&1 && command -v gcc >/dev/null 2>&1; then
            (
                cd "${remote_tmp}/exodriver/liblabjackusb"
                make clean >/dev/null 2>&1 || true
                make
                sudo_cmd make install
                sudo_cmd ldconfig
            ) || echo "Warning: Could not build liblabjackusb (ensure libusb-1.0-0-dev is installed on cart)." >&2
        else
            echo "Warning: gcc/make not present on cart; skipping liblabjackusb source build." >&2
        fi
    else
        echo "--> liblabjackusb is already installed on ${REMOTE_TARGET}."
    fi

    if [[ -f "${remote_tmp}/exodriver/90-labjack.rules" ]]; then
        echo "--> Installing LabJack udev rules (/etc/udev/rules.d/90-labjack.rules) ..."
        sudo_cmd cp "${remote_tmp}/exodriver/90-labjack.rules" /etc/udev/rules.d/90-labjack.rules
        sudo_cmd chmod 644 /etc/udev/rules.d/90-labjack.rules
        sudo_cmd udevadm control --reload-rules || true
        sudo_cmd udevadm trigger || true
    fi
fi

echo "--> Ensuring user '${REMOTE_USER}' is in 'dialout' and 'adm' groups ..."
sudo_cmd usermod -aG dialout "${REMOTE_USER}" 2>/dev/null || true
sudo_cmd usermod -aG adm "${REMOTE_USER}" 2>/dev/null || true

echo "--> Verifying installed Python modules on ${REMOTE_TARGET} ..."
python3 -c "import websockets, serial, u6; print('SUCCESS: websockets, pyserial, and LabJackPython (u6) imported cleanly!')"

rm -rf "${remote_tmp}"
EOF

    echo "==> Offline dependency installation complete! Restarting '${SERVICE_NAME}' ..."
    do_restart
}

interactive_menu() {
    while true; do
        local connected=0
        local conn_label="DISCONNECTED (unreachable)"
        if is_valve_cart_reachable; then
            connected=1
            conn_label="CONNECTED"
        fi

        echo ""
        echo "=========================================="
        echo " Service Manager: ${SERVICE_NAME} (${REMOTE_TARGET})"
        echo " Connection: ${conn_label}"
        echo "=========================================="
        if [[ "${connected}" == "1" ]]; then
            echo "1) Check status & journald logs"
            echo "2) Follow live journald logs (journalctl -f)"
            echo "3) Restart service"
            echo "4) Enable service (autostart at boot)"
            echo "5) Disable service"
            echo "6) Start service"
            echo "7) Stop service"
            echo "8) Push & install dependencies on valve cart (offline via SSH)"
        else
            echo " [!] Valve cart is unreachable. Remote actions are disabled."
        fi
        echo "f) Fetch/cache dependencies locally (tools/vendor/)"
        echo "d) Direct Ethernet manager"
        echo "r) Re-check connection"
        echo "q) Quit"
        echo "------------------------------------------"
        if [[ "${connected}" == "1" ]]; then
            read -r -p "Select an option [1-8, f, d, r, q]: " choice
        else
            read -r -p "Select an option [f, d, r, q]: " choice
        fi

        case "${choice}" in
            f|F)
                set +e
                do_fetch_deps
                local f_rc=$?
                set -e
                if [[ ${f_rc} -ne 0 ]]; then
                    echo "Dependency fetch exited with status ${f_rc}." >&2
                fi
                echo ""
                read -r -p "Press Enter to return to the main menu..." _
                continue
                ;;
            d|D)
                set +e
                "${SCRIPT_DIR}/direct-ethernet.sh"
                local d_rc=$?
                set -e
                if [[ ${d_rc} -ne 0 && ${d_rc} -ne 130 ]]; then
                    echo "Direct Ethernet manager exited with status ${d_rc}." >&2
                fi
                echo ""
                read -r -p "Press Enter to return to the main menu..." _
                continue
                ;;
            r|R)
                continue
                ;;
            q|Q)
                exit 0
                ;;
            1|2|3|4|5|6|7|8)
                if [[ "${connected}" != "1" ]]; then
                    echo "Error: Not connected to valve cart (${REMOTE_TARGET}). Cannot perform this action." >&2
                    echo ""
                    read -r -p "Press Enter to return to the main menu..." _
                    continue
                fi
                ;;
            *)
                echo "Invalid selection: ${choice}" >&2
                continue
                ;;
        esac

        set +e
        case "${choice}" in
            1) do_status ;;
            2)
                trap ':' INT
                do_logs "-f"
                trap - INT
                ;;
            3) do_restart ;;
            4) do_enable ;;
            5) do_disable ;;
            6) do_start ;;
            7) do_stop ;;
            8) do_push_deps ;;
        esac
        local rc=$?
        set -e

        if [[ ${rc} -ne 0 && ${rc} -ne 130 ]]; then
            echo "Command exited with status ${rc}." >&2
        fi
        echo ""
        read -r -p "Press Enter to return to the main menu..." _
    done
}

COMMAND="${1:-}"
if [[ -z "${COMMAND}" ]]; then
    interactive_menu
    exit 0
fi

shift || true
case "${COMMAND}" in
    status)
        require_connection
        do_status
        ;;
    restart)
        require_connection
        do_restart
        ;;
    start)
        require_connection
        do_start
        ;;
    stop)
        require_connection
        do_stop
        ;;
    logs|journal|journald)
        require_connection
        do_logs "${1:-}"
        ;;
    follow|-f)
        require_connection
        do_logs "-f"
        ;;
    enable)
        require_connection
        do_enable
        ;;
    disable)
        require_connection
        do_disable
        ;;
    fetch-deps|--fetch-deps)
        do_fetch_deps
        ;;
    push-deps|--push-deps|deps|install-deps)
        require_connection
        do_push_deps
        ;;
    direct-eth|--direct-eth|direct-ethernet)
        "${SCRIPT_DIR}/direct-ethernet.sh" "$@"
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
