#!/usr/bin/env bash
# Server-side detector for direct Ethernet connections (PC-to-PC or PC-to-hub)
# without a router/DHCP server in-between.
#
# When a wired Ethernet link is up (carrier=1) and no router/gateway is reachable,
# this script ensures the server's Ethernet interface has the expected fallback
# IPv4 address (default: 192.168.0.1/24) so the client can reach SSH (22) and
# the Flow Control WebSocket server (8080).
set -euo pipefail

SERVER_IP="${SERVER_IP:-192.168.0.1}"
SERVER_PREFIX="${SERVER_PREFIX:-24}"
ROUTER_IPS="${ROUTER_IPS:-192.168.0.254 192.168.0.253}"
ETH_IFACE="${ETH_IFACE:-}"
POLL_INTERVAL="${POLL_INTERVAL:-3}"
PING_TIMEOUT="${PING_TIMEOUT:-1}"
STATE_FILE="${STATE_FILE:-/run/flow-control-direct-eth.state}"
NM_CON_NAME="${NM_CON_NAME:-fcs-direct-eth-server}"

MODE="once"
APPLY=0
VERBOSE=0

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Detect whether the server is connected directly over Ethernet (PC-to-PC or
PC-to-hub) without a router in-between, and optionally configure a fallback
static IP (${SERVER_IP}/${SERVER_PREFIX}).

Options:
  --once            Run a single detection pass and exit (default)
  --watch           Continuously monitor link/router state every ${POLL_INTERVAL}s
  --apply           Apply fallback IP (${SERVER_IP}/${SERVER_PREFIX}) when direct mode is detected
  --restore         Remove any fallback IP applied by this script and restore DHCP/auto
  --iface <name>    Explicitly specify the wired Ethernet interface
  --ip <addr>       Fallback IPv4 address (default: ${SERVER_IP})
  --prefix <len>    Fallback IPv4 CIDR prefix length (default: ${SERVER_PREFIX})
  -v, --verbose     Print detailed diagnostic output
  -h, --help        Show this help message

Exit codes (--once mode):
  0   Direct Ethernet (no router) detected
  1   Error (e.g., no Ethernet interface found)
  2   Router/gateway is reachable (normal network mode)
  3   Ethernet link is down (no carrier / cable unplugged)
EOF
}

log() {
    echo "[direct-eth] $*"
}

vlog() {
    if [[ "${VERBOSE}" == "1" ]]; then
        echo "[direct-eth][debug] $*"
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --once)
            MODE="once"
            shift
            ;;
        --watch)
            MODE="watch"
            APPLY=1
            shift
            ;;
        --apply)
            APPLY=1
            shift
            ;;
        --restore)
            MODE="restore"
            shift
            ;;
        --iface)
            ETH_IFACE="${2:-}"
            shift 2
            ;;
        --ip)
            SERVER_IP="${2:-}"
            shift 2
            ;;
        --prefix)
            SERVER_PREFIX="${2:-}"
            shift 2
            ;;
        -v|--verbose)
            VERBOSE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

detect_eth_iface() {
    if [[ -n "${ETH_IFACE}" ]]; then
        if [[ -d "/sys/class/net/${ETH_IFACE}" ]]; then
            echo "${ETH_IFACE}"
            return 0
        fi
        echo "Error: Specified interface '${ETH_IFACE}' does not exist." >&2
        return 1
    fi

    local dev name carrier
    local fallback=""
    for dev in /sys/class/net/*; do
        name="$(basename "${dev}")"
        [[ "${name}" == "lo" ]] && continue
        # Skip virtual, bridge, docker, veth, tun/tap interfaces
        [[ ! -d "${dev}/device" ]] && continue
        # Skip wireless interfaces
        [[ -d "${dev}/wireless" || -d "${dev}/phy80211" ]] && continue
        # Type 1 is Ethernet (ARPHRD_ETHER)
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

iface_has_ip() {
    local iface="$1"
    local ip_cidr="$2"
    ip -4 -o addr show dev "${iface}" 2>/dev/null | awk '{print $4}' | grep -Fxq "${ip_cidr}"
}

get_iface_ipv4s() {
    local iface="$1"
    ip -4 -o addr show dev "${iface}" 2>/dev/null | awk '{print $4}' | tr '\n' ' ' | sed 's/ $//'
}

probe_host_on_iface() {
    local iface="$1"
    local target="$2"
    if command -v arping >/dev/null 2>&1; then
        if arping -c 1 -w "${PING_TIMEOUT}" -I "${iface}" "${target}" >/dev/null 2>&1; then
            return 0
        fi
    fi
    if ping -c 1 -W "${PING_TIMEOUT}" -I "${iface}" "${target}" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

# Returns 0 if a router/gateway is reachable on iface, 1 if no router is detected.
router_is_reachable() {
    local iface="$1"
    local gw
    gw="$(ip -4 route show default dev "${iface}" 2>/dev/null | awk '/default via/ {print $3; exit}')"

    if [[ -n "${gw}" ]]; then
        vlog "Found default gateway ${gw} on ${iface}; probing..."
        if probe_host_on_iface "${iface}" "${gw}"; then
            vlog "Default gateway ${gw} responded."
            return 0
        fi
        vlog "Default gateway ${gw} did NOT respond."
    else
        vlog "No default gateway configured on ${iface}."
    fi

    local r_ip
    for r_ip in ${ROUTER_IPS}; do
        # Only probe if the interface has an IP or link-local, or via arping
        if probe_host_on_iface "${iface}" "${r_ip}"; then
            vlog "Known router/CPE ${r_ip} responded on ${iface}."
            return 0
        fi
    done

    return 1
}

apply_fallback_ip() {
    local iface="$1"
    local target_cidr="${SERVER_IP}/${SERVER_PREFIX}"

    if iface_has_ip "${iface}" "${target_cidr}"; then
        vlog "${iface} already has ${target_cidr}."
        return 0
    fi

    log "Applying direct-Ethernet fallback IP ${target_cidr} on ${iface}..."
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
    else
        ip link set dev "${iface}" up
        ip addr replace "${target_cidr}" dev "${iface}"
    fi

    echo "applied:${iface}:${target_cidr}" > "${STATE_FILE}"
    log "Direct-Ethernet mode active on ${iface} (${target_cidr})."
}

restore_normal_network() {
    local iface="$1"
    local target_cidr="${SERVER_IP}/${SERVER_PREFIX}"
    local did_change=0

    if nm_manages_iface "${iface}" && nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "${NM_CON_NAME}"; then
        log "Deactivating NetworkManager direct-Ethernet profile '${NM_CON_NAME}' on ${iface}..."
        nmcli connection down "${NM_CON_NAME}" >/dev/null 2>&1 || true
        nmcli connection delete "${NM_CON_NAME}" >/dev/null 2>&1 || true
        nmcli device connect "${iface}" >/dev/null 2>&1 || true
        did_change=1
    elif [[ -f "${STATE_FILE}" ]] && grep -q "^applied:${iface}:${target_cidr}$" "${STATE_FILE}"; then
        log "Removing temporary fallback IP ${target_cidr} from ${iface}..."
        ip addr del "${target_cidr}" dev "${iface}" 2>/dev/null || true
        if command -v networkctl >/dev/null 2>&1; then
            networkctl reconfigure "${iface}" >/dev/null 2>&1 || true
        elif command -v dhclient >/dev/null 2>&1; then
            dhclient -1 "${iface}" >/dev/null 2>&1 || true
        fi
        did_change=1
    fi

    rm -f "${STATE_FILE}"
    if [[ "${did_change}" == "1" ]]; then
        log "Restored normal network configuration on ${iface}."
    else
        vlog "No temporary direct-Ethernet configuration to restore on ${iface}."
    fi
}

check_once() {
    local iface
    if ! iface="$(detect_eth_iface)"; then
        echo "ERROR: No wired Ethernet interface found." >&2
        return 1
    fi

    local carrier operstate addrs
    carrier="$(cat "/sys/class/net/${iface}/carrier" 2>/dev/null || echo 0)"
    operstate="$(cat "/sys/class/net/${iface}/operstate" 2>/dev/null || echo unknown)"
    addrs="$(get_iface_ipv4s "${iface}")"

    if [[ "${carrier}" != "1" ]]; then
        log "STATUS=NO_LINK iface=${iface} operstate=${operstate} ipv4=\"${addrs}\""
        return 3
    fi

    if router_is_reachable "${iface}"; then
        log "STATUS=ROUTER_PRESENT iface=${iface} operstate=${operstate} ipv4=\"${addrs}\""
        if [[ "${APPLY}" == "1" && -f "${STATE_FILE}" ]]; then
            restore_normal_network "${iface}"
        fi
        return 2
    fi

    log "STATUS=DIRECT_ETHERNET iface=${iface} operstate=${operstate} ipv4=\"${addrs}\""
    if [[ "${APPLY}" == "1" ]]; then
        apply_fallback_ip "${iface}"
    fi
    return 0
}

if [[ "${MODE}" == "restore" ]]; then
    iface="$(detect_eth_iface)" || exit 1
    restore_normal_network "${iface}"
    exit 0
fi

if [[ "${MODE}" == "once" ]]; then
    check_once
    exit $?
fi

# Watch mode (for systemd service)
log "Starting direct-Ethernet monitor (poll=${POLL_INTERVAL}s, fallback=${SERVER_IP}/${SERVER_PREFIX})..."
LAST_STATE=""
while true; do
    set +e
    OUT="$(check_once 2>&1)"
    RC=$?
    set -e

    CURRENT_STATE="rc_${RC}"
    if [[ "${CURRENT_STATE}" != "${LAST_STATE}" || "${VERBOSE}" == "1" ]]; then
        echo "${OUT}"
        LAST_STATE="${CURRENT_STATE}"
    fi

    sleep "${POLL_INTERVAL}"
done
