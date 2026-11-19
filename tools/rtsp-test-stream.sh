#!/usr/bin/env bash
# ==============================================================================
# RTSP H.264 Test Stream for Flow-Control-System-Client (Video.qml)
#
# Starts a local MediaMTX RTSP server (TCP + UDP) and pushes a low-latency
# H.264 test stream via ffmpeg to:
#   rtsp://127.0.0.1:<PORT>/cam/realmonitor
#
# Usage:
#   ./tools/rtsp-test-stream.sh [--port 8554] [--res 1280x720] [--fps 30] [--input /dev/video0|file.mp4]
#
# In Flow-Control-System-Client:
#   1. Open Settings -> IP Camera -> Manual IP / RTSP URL:
#        rtsp://127.0.0.1:8554/cam/realmonitor
#      (or just "127.0.0.1:8554" with empty password)
#   2. In Video view sidebar, set Mode: Manual and click Connect.
# ==============================================================================
set -euo pipefail

PORT="8554"
RES="1280x720"
FPS="30"
INPUT=""
MEDIAMTX_VERSION="v1.11.3"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/fcs-rtsp-test"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)  PORT="$2"; shift 2 ;;
        --res)   RES="$2"; shift 2 ;;
        --fps)   FPS="$2"; shift 2 ;;
        --input) INPUT="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,16p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "Error: ffmpeg is required but not found in PATH." >&2
    exit 1
fi

# Resolve or download mediamtx (single static binary supporting RTSP TCP & UDP)
MEDIAMTX_BIN="$(command -v mediamtx || true)"
if [[ -z "$MEDIAMTX_BIN" ]]; then
    MEDIAMTX_BIN="$CACHE_DIR/mediamtx"
    if [[ ! -x "$MEDIAMTX_BIN" ]]; then
        mkdir -p "$CACHE_DIR"
        ARCH="$(uname -m)"
        case "$ARCH" in
            x86_64|amd64)  MTX_ARCH="amd64" ;;
            aarch64|arm64) MTX_ARCH="arm64v8" ;;
            *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
        esac
        TARBALL="mediamtx_${MEDIAMTX_VERSION}_linux_${MTX_ARCH}.tar.gz"
        URL="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/${TARBALL}"
        echo "[RTSP Test] Downloading mediamtx ${MEDIAMTX_VERSION} (${MTX_ARCH})..."
        curl -fsSL "$URL" -o "$CACHE_DIR/$TARBALL"
        tar -xzf "$CACHE_DIR/$TARBALL" -C "$CACHE_DIR" mediamtx
        rm -f "$CACHE_DIR/$TARBALL"
        chmod +x "$MEDIAMTX_BIN"
    fi
fi

MTX_CFG="$(mktemp /tmp/mediamtx_test_XXXXXX.yml)"
cat > "$MTX_CFG" <<EOF
logLevel: warn
writeQueueSize: 4096
rtspAddress: :${PORT}
rtspTransports: [udp, tcp]
rtmp: no
hls: no
webrtc: no
srt: no
paths:
  all_others:
EOF

MTX_PID=""
FFMPEG_PID=""

cleanup() {
    echo ""
    echo "[RTSP Test] Stopping test stream..."
    [[ -n "$FFMPEG_PID" ]] && kill "$FFMPEG_PID" 2>/dev/null || true
    [[ -n "$MTX_PID" ]] && kill "$MTX_PID" 2>/dev/null || true
    rm -f "$MTX_CFG"
}
trap cleanup INT TERM EXIT

"$MEDIAMTX_BIN" "$MTX_CFG" &
MTX_PID=$!
sleep 1

STREAM_URL="rtsp://127.0.0.1:${PORT}/cam/realmonitor"
echo "========================================================================"
echo "  RTSP H.264 Test Stream Active (TCP + UDP)"
echo "  URL:  ${STREAM_URL}"
echo "  Res:  ${RES} @ ${FPS} fps (H.264 zerolatency)"
echo ""
echo "  Client Settings -> Manual IP / RTSP URL:  ${STREAM_URL}"
echo "  Sidebar -> Mode: Manual -> Connect"
echo "  Press Ctrl+C to stop."
echo "========================================================================"

if [[ -n "$INPUT" ]]; then
    if [[ "$INPUT" == /dev/video* ]]; then
        ffmpeg -loglevel warning -f v4l2 -video_size "$RES" -framerate "$FPS" -i "$INPUT" \
            -c:v libx264 -preset ultrafast -tune zerolatency -x264-params sliced-threads=0 -pix_fmt yuv420p \
            -b:v 6M -maxrate 6M -bufsize 3M -g 15 -keyint_min 15 -sc_threshold 0 -bf 0 \
            -f rtsp -rtsp_transport tcp "$STREAM_URL" &
    else
        ffmpeg -loglevel warning -re -stream_loop -1 -i "$INPUT" \
            -c:v libx264 -preset ultrafast -tune zerolatency -x264-params sliced-threads=0 -pix_fmt yuv420p \
            -b:v 6M -maxrate 6M -bufsize 3M -g 15 -keyint_min 15 -sc_threshold 0 -bf 0 \
            -an -f rtsp -rtsp_transport tcp "$STREAM_URL" &
    fi
else
    ffmpeg -loglevel warning -re -f lavfi -i "testsrc2=size=${RES}:rate=${FPS}" \
        -c:v libx264 -preset ultrafast -tune zerolatency -x264-params sliced-threads=0 -pix_fmt yuv420p \
        -b:v 6M -maxrate 6M -bufsize 3M -g 15 -keyint_min 15 -sc_threshold 0 -bf 0 \
        -f rtsp -rtsp_transport tcp "$STREAM_URL" &
fi
FFMPEG_PID=$!

wait "$FFMPEG_PID"
