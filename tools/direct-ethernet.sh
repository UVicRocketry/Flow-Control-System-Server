#!/usr/bin/env bash
# Client-side tool to initiate, inspect, and tear down direct Ethernet
# connections (PC-to-PC or PC-to-hub without a router) to the Valve Cart server,
# and to install/trigger the server-side detection script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

REMOTE_USER="${REMOTE_USER:-uvr}"
REMOTE_HOST="${REMOTE_HOST:-192.168.0.1}"
REMOTE_PORT="${REMOTE_PORT:-22}"
REMOTE_TARGET="${REMOTE_USER}@${REMOTE_HOST}"
REMOTE_DIR="${REMOTE_DIR:-/home/${REMOTE_USER}/Flow-Control-System-Server}"
DETECT_SERVICE_NAME="${DETECT_SERVICE_NAME:-flow-control-direct-eth.service}"

CLIENT_IFACE="${CLIENT_IFACE:-}"
CLIENT_IP="${CLIENT_IP:-192.168.0.10}"
NET_PREFIX="${NET_PREFIX:-24}"
NM_CON_NAME="${NM_CON_NAME:-fcs-direct-eth}"
WS_PORT="${WS_PORT:-8080}"
STATE_FILE="${STATE_FILE:-/tmp/fcs-direct-eth-client.state}"

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

cleanup() {
    if [[ -n "${CTL_DIR}" && -S "${CTL_DIR}/mux" ]]; then
        ssh -O exit -o ControlPath="${CTL_DIR}/mux" "${REMOTE_TARGET}" 2>/dev/null || true
    fi
    rm -rf "${ASKPASS_SCRIPT}" "${CTL_DIR}" 2>/dev/null || true
}
trap cleanup EXIT

init_ssh() {
    if [[ ! -f "${PASSWORD_FILE}" ]]; then
        echo "Error: Password file not found at '${PASSWORD_FILE}'." >&2
        echo "Create '${SCRIPT_DIR}/password.txt' (or '${REPO_ROOT}/password.txt') with the SSH/sudo password for ${REMOTE_TARGET}." >&2
        return 1
    fi

    SSH_PASSWORD="$(tr -d '\r\n' < "${PASSWORD_FILE}")"
    if [[ -z "${SSH_PASSWORD}" ]]; then
        echo "Error: Password file '${PASSWORD_FILE}' is empty." >&2
        return 1
    fi

    if [[ -z "${ASKPASS_SCRIPT}" ]]; then
        ASKPASS_SCRIPT="$(mktemp)"
        CTL_DIR="$(mktemp -d)"
        cat > "${ASKPASS_SCRIPT}" <<EOF
#!/usr/bin/env bash
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
}

ssh_opts() {
    local opts=(
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
        opts+=(-v)
    fi
    printf '%s\n' "${opts[@]}"
}

run_ssh() {
    init_ssh || return 1
    local opts=()
    mapfile -t opts < <(ssh_opts)
    if command -v sshpass >/dev/null 2>&1; then
        sshpass -p "${SSH_PASSWORD}" ssh "${opts[@]}" "${REMOTE_TARGET}" "$@"
    else
        ssh "${opts[@]}" "${REMOTE_TARGET}" "$@"
    fi
}

run_remote_sudo() {
    init_ssh || return 1
    local escaped_pw
    escaped_pw="$(printf '%q' "${SSH_PASSWORD}")"
    run_ssh "printf '%s\n' ${escaped_pw} | sudo -S -p '' $*"
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [COMMAND] [OPTIONS]

Manage direct Ethernet connections (PC-to-PC or PC-to-hub without a router)
between this client PC and the Valve Cart server (${REMOTE_TARGET}).
If no command is given, an interactive menu is shown.

Commands:
  initiate [opts]   Configure client Ethernet for direct mode (${CLIENT_IP}/${NET_PREFIX})
                    and verify/trigger direct mode on the server
  restore [opts]    Restore normal DHCP/auto Ethernet config on the client
  status            Show client Ethernet state, server reachability, and server detection
  install-server    Deploy and enable '${DETECT_SERVICE_NAME}' on ${REMOTE_TARGET}
  server-detect     Run direct-Ethernet detection once on ${REMOTE_TARGET}
  help              Show this help message

Options:
  --iface <name>    Specify the client's wired Ethernet interface
  --ip <addr>       Client IPv4 address for direct mode (default: ${CLIENT_IP})
  --local           (initiate) Only configure client side; skip SSH check to server
  --server          (restore) Also run --restore on the server via SSH
EOF
}

detect_client_eth_iface() {
    if [[ -n "${CLIENT_IFACE}" ]]; then
        if [[ -d "/sys/class/net/${CLIENT_IFACE}" ]]; then
            echo "${CLIENT_IFACE}"
            return 0
        fi
        echo "Error: Specified client interface '${CLIENT_IFACE}' does not exist." >&2
        return 1
    fi

    local dev name carrier
    local fallback=""
    for dev in /sys/class/net/*; do
        name="$(basename "${dev}")"
        [[ "${name}" == "lo" ]] && continue
        [[ ! -d "${dev}/device" ]] && continue
        [[ -d "${dev}/wireless" || -d "${dev}/phy80211" ]] && continue
        if [[ -f "${dev}/type" ]] && [[ "$(cat "${dev}/type")" != "1" ]]; then
            continue
        fi
        carrier="$(cat "${dev}/carrier" 2>/dev/null || echo 0)"
        if [[ "${carrier}" == "1" ]]; then
            echo "${name}"
            return 0
        fi
        if [[ -z "${fallback}" ]]; then
            fallback="${name}"
        fi
    done

    if [[ -n "${fallback}" ]]; then
        echo "${fallback}"
        return 0
    fi

    echo "Error: No wired Ethernet interface found on this machine." >&2
    return 1
}

nm_manages_iface() {
    local iface="$1"
    command -v nmcli >/dev/null 2>&1 || return 1
    systemctl is-active --quiet NetworkManager 2>/dev/null || return 1
    local state
    state="$(nmcli -t -f DEVICE,STATE device status 2>/dev/null | awk -F: -v d="${iface}" '$1 == d {print $2; exit}')"
    [[ -n "${state}" && "${state}" != "unmanaged" ]]
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

do_initiate() {
    local local_only=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --iface) CLIENT_IFACE="${2:-}"; shift 2 ;;
            --ip) CLIENT_IP="${2:-}"; shift 2 ;;
            --local) local_only=1; shift ;;
            *) echo "Unknown option for initiate: $1" >&2; exit 1 ;;
        esac
    done

    local iface
    iface="$(detect_client_eth_iface)"

    local carrier
    carrier="$(cat "/sys/class/net/${iface}/carrier" 2>/dev/null || echo 0)"
    if [[ "${carrier}" != "1" ]]; then
        echo "Warning: Ethernet interface '${iface}' currently has no link carrier (cable unplugged or peer off)."
        echo "Proceeding to configure '${iface}' so it is ready as soon as link comes up..."
    fi

    local target_cidr="${CLIENT_IP}/${NET_PREFIX}"
    echo "==> Configuring client Ethernet '${iface}' for direct mode (${target_cidr}, no default gateway)..."

    if nm_manages_iface "${iface}"; then
        if nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "${NM_CON_NAME}"; then
            nmcli connection modify "${NM_CON_NAME}" \
                connection.interface-name "${iface}" \
                ipv4.method manual \
                ipv4.addresses "${target_cidr}" \
                ipv4.never-default yes \
                ipv6.method ignore >/dev/null
        else
            nmcli connection add type ethernet \
                con-name "${NM_CON_NAME}" \
                ifname "${iface}" \
                ipv4.method manual \
                ipv4.addresses "${target_cidr}" \
                ipv4.never-default yes \
                ipv6.method ignore >/dev/null
        fi
        nmcli connection up "${NM_CON_NAME}" >/dev/null
        echo "nm:${iface}:${target_cidr}" > "${STATE_FILE}"
    else
        sudo ip link set dev "${iface}" up
        sudo ip addr replace "${target_cidr}" dev "${iface}"
        echo "ip:${iface}:${target_cidr}" > "${STATE_FILE}"
    fi

    echo "==> Client '${iface}' configured with ${target_cidr}."

    if [[ "${local_only}" == "1" ]]; then
        return 0
    fi

    echo "==> Probing server at ${REMOTE_HOST}..."
    if ping -c 2 -W 1 -I "${iface}" "${REMOTE_HOST}" >/dev/null 2>&1; then
        echo "    [OK] Ping to ${REMOTE_HOST} succeeded."
    else
        echo "    [WARN] ${REMOTE_HOST} did not reply to ping yet."
        echo "           Ensure the server has '${DETECT_SERVICE_NAME}' installed (run 'install-server' when connected)"
        echo "           or wait a few seconds for the server's detector to apply ${REMOTE_HOST}."
    fi

    if check_tcp_port "${REMOTE_HOST}" "${REMOTE_PORT}"; then
        echo "    [OK] SSH port ${REMOTE_PORT} is reachable on ${REMOTE_HOST}."
        if [[ -f "${PASSWORD_FILE}" ]]; then
            echo "==> Confirming direct-Ethernet state on ${REMOTE_TARGET}..."
            run_remote_sudo "bash '${REMOTE_DIR}/contents/code/direct-eth-detect.sh' --once --apply" || true
        fi
    else
        echo "    [INFO] SSH port ${REMOTE_PORT} not reachable yet on ${REMOTE_HOST}."
    fi

    if check_tcp_port "${REMOTE_HOST}" "${WS_PORT}"; then
        echo "    [OK] WebSocket port ${WS_PORT} is listening (ws://${REMOTE_HOST}:${WS_PORT}/ws)."
    else
        echo "    [INFO] WebSocket port ${WS_PORT} is not listening (flow-control-server.service may be stopped)."
    fi
}

do_restore() {
    local restore_server=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --iface) CLIENT_IFACE="${2:-}"; shift 2 ;;
            --server) restore_server=1; shift ;;
            *) echo "Unknown option for restore: $1" >&2; exit 1 ;;
        esac
    done

    if [[ "${restore_server}" == "1" ]]; then
        echo "==> Restoring normal network configuration on server ${REMOTE_TARGET}..."
        run_remote_sudo "bash '${REMOTE_DIR}/contents/code/direct-eth-detect.sh' --restore" || true
    fi

    local iface
    iface="$(detect_client_eth_iface)"

    echo "==> Restoring normal DHCP/auto Ethernet configuration on client '${iface}'..."
    if nm_manages_iface "${iface}" && nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "${NM_CON_NAME}"; then
        nmcli connection down "${NM_CON_NAME}" >/dev/null 2>&1 || true
        nmcli connection delete "${NM_CON_NAME}" >/dev/null 2>&1 || true
        nmcli device connect "${iface}" >/dev/null 2>&1 || true
    else
        if [[ -f "${STATE_FILE}" ]]; then
            local saved_cidr
            saved_cidr="$(awk -F: '{print $3}' "${STATE_FILE}")"
            if [[ -n "${saved_cidr}" ]]; then
                sudo ip addr del "${saved_cidr}" dev "${iface}" 2>/dev/null || true
            fi
        fi
        if command -v networkctl >/dev/null 2>&1; then
            sudo networkctl reconfigure "${iface}" >/dev/null 2>&1 || true
        elif command -v dhclient >/dev/null 2>&1; then
            sudo dhclient -1 "${iface}" >/dev/null 2>&1 || true
        fi
    fi

    rm -f "${STATE_FILE}"
    echo "==> Client '${iface}' restored to normal networking."
}

do_status() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --iface) CLIENT_IFACE="${2:-}"; shift 2 ;;
            *) echo "Unknown option for status: $1" >&2; exit 1 ;;
        esac
    done

    local iface carrier operstate addrs
    iface="$(detect_client_eth_iface)"
    carrier="$(cat "/sys/class/net/${iface}/carrier" 2>/dev/null || echo 0)"
    operstate="$(cat "/sys/class/net/${iface}/operstate" 2>/dev/null || echo unknown)"
    addrs="$(ip -4 -o addr show dev "${iface}" 2>/dev/null | awk '{print $4}' | tr '\n' ' ')"

    echo "==> Client Ethernet status:"
    echo "    Interface : ${iface}"
    echo "    Link      : carrier=${carrier} (${operstate})"
    echo "    IPv4      : ${addrs:-<none>}"
    if nm_manages_iface "${iface}" && nmcli -t -f NAME connection show --active 2>/dev/null | grep -Fxq "${NM_CON_NAME}"; then
        echo "    Direct NM : active (${NM_CON_NAME})"
    elif [[ -f "${STATE_FILE}" ]]; then
        echo "    Direct IP : active ($(cat "${STATE_FILE}"))"
    else
        echo "    Direct    : inactive"
    fi

    echo ""
    echo "==> Server reachability (${REMOTE_HOST}):"
    if ping -c 1 -W 1 "${REMOTE_HOST}" >/dev/null 2>&1; then
        echo "    Ping      : OK"
    else
        echo "    Ping      : unreachable"
    fi

    if check_tcp_port "${REMOTE_HOST}" "${REMOTE_PORT}"; then
        echo "    SSH (${REMOTE_PORT})  : open"
        if [[ -f "${PASSWORD_FILE}" ]]; then
            echo ""
            echo "==> Server-side detection (${REMOTE_TARGET}):"
            run_remote_sudo "bash '${REMOTE_DIR}/contents/code/direct-eth-detect.sh' --once" || true
            echo "==> Server service (${DETECT_SERVICE_NAME}):"
            run_remote_sudo "systemctl --no-pager --full status '${DETECT_SERVICE_NAME}'" || true
        fi
    else
        echo "    SSH (${REMOTE_PORT})  : closed/unreachable"
    fi

    if check_tcp_port "${REMOTE_HOST}" "${WS_PORT}"; then
        echo "    WS (${WS_PORT}) : open (ws://${REMOTE_HOST}:${WS_PORT}/ws)"
    else
        echo "    WS (${WS_PORT}) : closed/unreachable"
    fi
}

do_install_server() {
    local local_script="${REPO_ROOT}/contents/code/direct-eth-detect.sh"
    local local_unit="${REPO_ROOT}/contents/config/${DETECT_SERVICE_NAME}"

    if [[ ! -f "${local_script}" || ! -f "${local_unit}" ]]; then
        echo "Error: Missing '${local_script}' or '${local_unit}'." >&2
        exit 1
    fi

    echo "==> Deploying direct-Ethernet detector to ${REMOTE_TARGET}..."
    run_ssh "mkdir -p '${REMOTE_DIR}/contents/code' '${REMOTE_DIR}/contents/config'"

    init_ssh
    local opts=()
    mapfile -t opts < <(ssh_opts)
    if command -v sshpass >/dev/null 2>&1; then
        sshpass -p "${SSH_PASSWORD}" scp "${opts[@]}" "${local_script}" "${REMOTE_TARGET}:${REMOTE_DIR}/contents/code/direct-eth-detect.sh"
        sshpass -p "${SSH_PASSWORD}" scp "${opts[@]}" "${local_unit}" "${REMOTE_TARGET}:${REMOTE_DIR}/contents/config/${DETECT_SERVICE_NAME}"
    else
        scp "${opts[@]}" "${local_script}" "${REMOTE_TARGET}:${REMOTE_DIR}/contents/code/direct-eth-detect.sh"
        scp "${opts[@]}" "${local_unit}" "${REMOTE_TARGET}:${REMOTE_DIR}/contents/config/${DETECT_SERVICE_NAME}"
    fi

    echo "==> Installing and enabling '${DETECT_SERVICE_NAME}' on ${REMOTE_TARGET}..."
    run_remote_sudo "chmod +x '${REMOTE_DIR}/contents/code/direct-eth-detect.sh'"
    run_remote_sudo "cp '${REMOTE_DIR}/contents/config/${DETECT_SERVICE_NAME}' '/etc/systemd/system/${DETECT_SERVICE_NAME}'"
    run_remote_sudo "chmod 644 '/etc/systemd/system/${DETECT_SERVICE_NAME}'"
    run_remote_sudo "systemctl daemon-reload"
    run_remote_sudo "systemctl enable --now '${DETECT_SERVICE_NAME}'"
    run_remote_sudo "systemctl --no-pager --full status '${DETECT_SERVICE_NAME}'" || true

    echo "==> '${DETECT_SERVICE_NAME}' is installed and running on ${REMOTE_TARGET}."
}

do_server_detect() {
    echo "==> Running direct-Ethernet detection on ${REMOTE_TARGET}..."
    run_remote_sudo "bash '${REMOTE_DIR}/contents/code/direct-eth-detect.sh' --once --verbose"
}

interactive_menu() {
    echo "=========================================="
    echo " Direct Ethernet Manager (${REMOTE_TARGET})"
    echo "=========================================="
    echo "1) Initiate direct Ethernet mode (client -> server)"
    echo "2) Restore normal DHCP/auto Ethernet mode (client)"
    echo "3) Check status (client + server reachability)"
    echo "4) Install & enable server-side detector service"
    echo "5) Run server-side detection once (via SSH)"
    echo "q) Quit"
    echo "------------------------------------------"
    read -r -p "Select an option [1-5, q]: " choice
    case "${choice}" in
        1) do_initiate ;;
        2) do_restore ;;
        3) do_status ;;
        4) do_install_server ;;
        5) do_server_detect ;;
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
    initiate|up|connect|start)
        do_initiate "$@"
        ;;
    restore|down|disconnect|stop)
        do_restore "$@"
        ;;
    status|detect)
        do_status "$@"
        ;;
    install-server|install)
        do_install_server "$@"
        ;;
    server-detect)
        do_server_detect "$@"
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
