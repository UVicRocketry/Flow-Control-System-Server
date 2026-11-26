#!/usr/bin/env bash
# Client-side tool to initiate, inspect, and tear down direct Ethernet
# connections (PC-to-PC or PC-to-hub without a router) to the Valve Cart server
# and IP camera, and to install/trigger the server-side detection script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

source "${SCRIPT_DIR}/_common.sh"

DETECT_SERVICE_NAME="${DETECT_SERVICE_NAME:-flow-control-direct-eth.service}"

CLIENT_IFACE="${CLIENT_IFACE:-}"
CLIENT_IP="${CLIENT_IP:-192.168.0.10}"
CLIENT_ALT_CAM_IP="${CLIENT_ALT_CAM_IP:-192.168.1.10}"
NET_PREFIX="${NET_PREFIX:-24}"
NM_CON_NAME="${NM_CON_NAME:-fcs-direct-eth}"
WS_PORT="${WS_PORT:-8080}"
CAMERA_IP="${CAMERA_IP:-192.168.0.103}"
CAMERA_CANDIDATE_IPS="${CAMERA_CANDIDATE_IPS:-192.168.0.103 192.168.1.108 192.168.0.108 192.168.1.103}"
CAMERA_RTSP_PORT="${CAMERA_RTSP_PORT:-554}"
CAMERA_HTTP_PORT="${CAMERA_HTTP_PORT:-80}"
STATE_FILE="${STATE_FILE:-/tmp/fcs-direct-eth-client.state}"

usage() {
    cat <<EOF
Usage: $(basename "$0") [COMMAND] [OPTIONS]

Manage direct Ethernet connections (PC-to-PC or PC-to-hub without a router)
between this client PC, the Valve Cart server (${REMOTE_TARGET}), and the
IP camera (default: ${CAMERA_IP}:${CAMERA_RTSP_PORT}).
If no command is given, an interactive menu is shown.

Commands:
  initiate [opts]   Configure client Ethernet for direct mode (${CLIENT_IP}/${NET_PREFIX}),
                    verify/trigger direct mode on the server, and configure IP camera
  camera [opts]     Discover, verify, and configure direct Ethernet access to the IP camera
  restore [opts]    Restore normal DHCP/auto Ethernet config on the client
  status            Show client Ethernet state, server reachability, and camera status
  install-server    Deploy and enable '${DETECT_SERVICE_NAME}' on ${REMOTE_TARGET}
  server-detect     Run direct-Ethernet detection once on ${REMOTE_TARGET}
  help              Show this help message

Options:
  --iface <name>    Specify the client's wired Ethernet interface
  --ip <addr>       Client IPv4 address for direct mode (default: ${CLIENT_IP})
  --camera-ip <ip>  Expected IP camera address (default: ${CAMERA_IP})
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

iface_has_subnet() {
    local iface="$1"
    local prefix3="$2"
    ip -4 -o addr show dev "${iface}" 2>/dev/null | awk '{print $4}' | grep -q "^${prefix3}\."
}

ensure_client_direct_ip() {
    local iface="$1"
    local extra_cidr="${2:-}"
    local target_cidr="${CLIENT_IP}/${NET_PREFIX}"
    local alt_cidr="${CLIENT_ALT_CAM_IP}/${NET_PREFIX}"
    local addrs_csv="${target_cidr}"
    if [[ -n "${alt_cidr}" && "${alt_cidr}" != "${target_cidr}" ]]; then
        addrs_csv="${addrs_csv},${alt_cidr}"
    fi
    if [[ -n "${extra_cidr}" && "${extra_cidr}" != "${target_cidr}" && "${extra_cidr}" != "${alt_cidr}" ]]; then
        addrs_csv="${addrs_csv},${extra_cidr}"
    fi

    if nm_manages_iface "${iface}"; then
        if nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "${NM_CON_NAME}"; then
            nmcli connection modify "${NM_CON_NAME}" \
                connection.interface-name "${iface}" \
                ipv4.method manual \
                ipv4.addresses "${addrs_csv}" \
                ipv4.never-default yes \
                ipv6.method ignore >/dev/null
        else
            nmcli connection add type ethernet \
                con-name "${NM_CON_NAME}" \
                ifname "${iface}" \
                ipv4.method manual \
                ipv4.addresses "${addrs_csv}" \
                ipv4.never-default yes \
                ipv6.method ignore >/dev/null
        fi
        nmcli connection up "${NM_CON_NAME}" >/dev/null
        echo "nm:${iface}:${addrs_csv}" > "${STATE_FILE}"
    else
        sudo ip link set dev "${iface}" up
        sudo ip addr replace "${target_cidr}" dev "${iface}"
        if [[ -n "${alt_cidr}" && "${alt_cidr}" != "${target_cidr}" ]]; then
            sudo ip addr replace "${alt_cidr}" dev "${iface}"
        fi
        if [[ -n "${extra_cidr}" && "${extra_cidr}" != "${target_cidr}" && "${extra_cidr}" != "${alt_cidr}" ]]; then
            sudo ip addr replace "${extra_cidr}" dev "${iface}"
        fi
        echo "ip:${iface}:${addrs_csv}" > "${STATE_FILE}"
    fi
}

discover_camera_ip() {
    local iface="$1"
    local candidates=()
    local c

    # Prioritize explicit CAMERA_IP first, then known candidate IPs
    candidates+=("${CAMERA_IP}")
    for c in ${CAMERA_CANDIDATE_IPS}; do
        if [[ "${c}" != "${CAMERA_IP}" ]]; then
            candidates+=("${c}")
        fi
    done

    # Also include any active ARP neighbors on the interface (excluding server and client)
    local neigh_ip
    while read -r neigh_ip; do
        [[ -z "${neigh_ip}" ]] && continue
        [[ "${neigh_ip}" == "${REMOTE_HOST}" || "${neigh_ip}" == "${CLIENT_IP}" || "${neigh_ip}" == "${CLIENT_ALT_CAM_IP}" ]] && continue
        candidates+=("${neigh_ip}")
    done < <(ip -4 neigh show dev "${iface}" 2>/dev/null | awk '/REACHABLE|STALE|DELAY|PERMANENT/ {print $1}')

    for c in "${candidates[@]}"; do
        if [[ "${c}" == 192.168.1.* ]] && ! iface_has_subnet "${iface}" "192\\.168\\.1"; then
            ensure_client_direct_ip "${iface}" 2>/dev/null || true
        fi
        if check_tcp_port "${c}" "${CAMERA_RTSP_PORT}"; then
            echo "${c}"
            return 0
        fi
    done

    # Fast non-blocking TCP port 554 subnet scan across 192.168.0.0/24 and 192.168.1.0/24
    if command -v python3 >/dev/null 2>&1; then
        local scanned_ip=""
        scanned_ip="$(python3 - "${CAMERA_RTSP_PORT}" "${REMOTE_HOST}" "${CLIENT_IP}" "${CLIENT_ALT_CAM_IP}" <<'PYEOF' || true
import errno, selectors, socket, sys

port = int(sys.argv[1])
excluded = set(sys.argv[2:]) | {
    "192.168.0.1", "192.168.0.10", "192.168.0.253", "192.168.0.254",
    "192.168.1.1", "192.168.1.10", "192.168.1.253", "192.168.1.254",
}
targets = [
    f"{prefix}{host}"
    for prefix in ("192.168.0.", "192.168.1.")
    for host in range(2, 253)
    if f"{prefix}{host}" not in excluded
]

for i in range(0, len(targets), 64):
    batch = targets[i : i + 64]
    sel = selectors.DefaultSelector()
    sockets = []
    try:
        for ip in batch:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)
            err = sock.connect_ex((ip, port))
            if err == 0:
                print(ip)
                sock.close()
                sys.exit(0)
            if err in (errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK):
                sel.register(sock, selectors.EVENT_WRITE, ip)
                sockets.append(sock)
            else:
                sock.close()
        import time
        deadline = time.monotonic() + 0.15
        while sockets and sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for key, _ in sel.select(timeout=remaining):
                sock = key.fileobj
                ip = key.data
                sel.unregister(sock)
                if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                    print(ip)
                    sys.exit(0)
    finally:
        sel.close()
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass
PYEOF
)"
        if [[ -n "${scanned_ip}" ]]; then
            echo "${scanned_ip}"
            return 0
        fi
    fi

    return 1
}

sync_client_camera_config() {
    local cam_ip="$1"
    local cfg_files=(
        "${REPO_ROOT}/../Flow-Control-System-Client/contents/config/config.json"
        "${XDG_CONFIG_HOME:-${HOME}/.config}/flow-control/config.json"
    )
    local cfg
    for cfg in "${cfg_files[@]}"; do
        if [[ -f "${cfg}" ]] && command -v python3 >/dev/null 2>&1; then
            python3 - "${cfg}" "${cam_ip}" <<'PYEOF' || true
import json, sys
from pathlib import Path

cfg_path = Path(sys.argv[1]).resolve()
cam_ip = sys.argv[2].strip()
try:
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
except Exception:
    sys.exit(0)

cam = data.get("camera")
if not isinstance(cam, dict):
    cam = {}
    data["camera"] = cam

changed = False
if cam.get("mode") != "auto":
    cam["mode"] = "auto"
    changed = True

if cam.get("manual_target") != cam_ip:
    cam["manual_target"] = cam_ip
    changed = True

if not cam.get("password"):
    if not cam.get("username"):
        cam["username"] = "admin"
    else:
        cam["username"] = "admin"
    cam["password"] = "admin"
    changed = True

if changed:
    cfg_path.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")
    print(f"    [OK] Updated client camera config: {cfg_path} (target={cam_ip}, mode={cam.get('mode')})")
PYEOF
        fi
    done
}

probe_and_configure_camera() {
    local iface="$1"
    echo "==> Probing IP camera on '${iface}' (expected: ${CAMERA_IP}:${CAMERA_RTSP_PORT})..."

    local found_ip=""
    if found_ip="$(discover_camera_ip "${iface}")"; then
        CAMERA_IP="${found_ip}"
        echo "    [OK] Camera discovered at ${CAMERA_IP} (RTSP port ${CAMERA_RTSP_PORT} open)."
        if check_tcp_port "${CAMERA_IP}" "${CAMERA_HTTP_PORT}"; then
            echo "    [OK] Camera HTTP config interface (${CAMERA_IP}:${CAMERA_HTTP_PORT}) is open."
        fi
        echo "    [OK] RTSP URL: rtsp://${CAMERA_IP}:${CAMERA_RTSP_PORT}/cam/realmonitor?channel=1&subtype=0"
        sync_client_camera_config "${CAMERA_IP}"
        return 0
    else
        echo "    [WARN] No IP camera responded on RTSP port ${CAMERA_RTSP_PORT} (checked: ${CAMERA_IP}, ${CAMERA_CANDIDATE_IPS})."
        echo "           Verify the camera is powered and connected to the Ethernet hub."
        return 1
    fi
}

do_initiate() {
    local local_only=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --iface) CLIENT_IFACE="${2:-}"; shift 2 ;;
            --ip) CLIENT_IP="${2:-}"; shift 2 ;;
            --camera-ip) CAMERA_IP="${2:-}"; shift 2 ;;
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
    local alt_cidr="${CLIENT_ALT_CAM_IP}/${NET_PREFIX}"
    echo "==> Configuring client Ethernet '${iface}' for direct mode (${target_cidr}, ${alt_cidr}, no default gateway)..."
    ensure_client_direct_ip "${iface}"
    echo "==> Client '${iface}' configured with ${target_cidr}, ${alt_cidr}."

    probe_and_configure_camera "${iface}" || true

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

do_camera() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --iface) CLIENT_IFACE="${2:-}"; shift 2 ;;
            --ip|--camera-ip) CAMERA_IP="${2:-}"; shift 2 ;;
            *) echo "Unknown option for camera: $1" >&2; exit 1 ;;
        esac
    done

    local iface
    iface="$(detect_client_eth_iface)"

    # Ensure client interface has dual-homed direct Ethernet IPs on 192.168.0.0/24 and 192.168.1.0/24
    if ! iface_has_subnet "${iface}" "192\\.168\\.0" || ! iface_has_subnet "${iface}" "192\\.168\\.1"; then
        echo "==> Configuring direct Ethernet subnets (${CLIENT_IP}/${NET_PREFIX}, ${CLIENT_ALT_CAM_IP}/${NET_PREFIX}) on '${iface}'..."
        ensure_client_direct_ip "${iface}"
    fi

    probe_and_configure_camera "${iface}"
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
            local saved_cidrs cidr
            saved_cidrs="$(awk -F: '{print $3}' "${STATE_FILE}")"
            IFS=',' read -r -a cidr_arr <<< "${saved_cidrs}"
            for cidr in "${cidr_arr[@]}"; do
                if [[ -n "${cidr}" ]]; then
                    sudo ip addr del "${cidr}" dev "${iface}" 2>/dev/null || true
                fi
            done
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
            --camera-ip) CAMERA_IP="${2:-}"; shift 2 ;;
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

    echo ""
    echo "==> IP Camera reachability:"
    local found_cam=""
    if found_cam="$(discover_camera_ip "${iface}")"; then
        echo "    Camera IP : ${found_cam}"
        echo "    RTSP (${CAMERA_RTSP_PORT}) : open (rtsp://${found_cam}:${CAMERA_RTSP_PORT}/cam/realmonitor?channel=1&subtype=0)"
        if check_tcp_port "${found_cam}" "${CAMERA_HTTP_PORT}"; then
            echo "    HTTP (${CAMERA_HTTP_PORT})  : open (http://${found_cam})"
        else
            echo "    HTTP (${CAMERA_HTTP_PORT})  : closed"
        fi
    else
        echo "    Camera IP : unreachable (checked ${CAMERA_IP}, ${CAMERA_CANDIDATE_IPS})"
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
    run_scp_to "${local_script}" "${REMOTE_DIR}/contents/code/direct-eth-detect.sh"
    run_scp_to "${local_unit}" "${REMOTE_DIR}/contents/config/${DETECT_SERVICE_NAME}"

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
    echo "1) Initiate direct Ethernet mode (client -> server & camera)"
    echo "2) Discover & configure IP camera (${CAMERA_IP})"
    echo "3) Restore normal DHCP/auto Ethernet mode (client)"
    echo "4) Check status (client + server + camera)"
    echo "5) Install & enable server-side detector service"
    echo "6) Run server-side detection once (via SSH)"
    echo "q) Quit"
    echo "------------------------------------------"
    read -r -p "Select an option [1-6, q]: " choice
    case "${choice}" in
        1) do_initiate ;;
        2) do_camera ;;
        3) do_restore ;;
        4) do_status ;;
        5) do_install_server ;;
        6) do_server_detect ;;
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
    camera|cam|connect-camera)
        do_camera "$@"
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
