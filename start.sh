#!/usr/bin/env bash
# Start all local services then launch the agent.
# Usage: ./start.sh [--install] [console|dev|start]
#   --install  install Python packages + download plugin model files (run once after cloning)
#   console    default — run locally with mic/speakers, no LiveKit server needed
#   dev        connect to a LiveKit room in development mode

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[info]${NC}  $*"; }
ok()      { echo -e "${GREEN}[ ok ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[warn]${NC}  $*"; }
die()     { echo -e "${RED}[fail]${NC}  $*" >&2; exit 1; }

# ── argument parsing ───────────────────────────────────────────────────────
INSTALL=false
AGENT_MODE="console"

for arg in "$@"; do
    case "$arg" in
        --install) INSTALL=true ;;
        *)         AGENT_MODE="$arg" ;;
    esac
done

# ── config ─────────────────────────────────────────────────────────────────
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
WHISPER_MODEL="${WHISPER_MODEL:-base}"  # tiny|base|small|medium|large-v3

# ── activate venv ──────────────────────────────────────────────────────────
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
else
    warn "No .venv found — using system Python. Run: python3 -m venv .venv && pip install -r requirements.txt"
fi

PYTHON="$(command -v python || command -v python3)"
[ -z "$PYTHON" ] && die "Python not found."

# ── one-time dependency install ────────────────────────────────────────────
if [ "$INSTALL" = true ]; then
    # System library required by sounddevice (console mic/speaker I/O)
    if ! ldconfig -p 2>/dev/null | grep -q "libportaudio"; then
        info "Installing libportaudio2 (required for mic/speaker I/O) ..."
        sudo apt-get install -y libportaudio2 || die "Failed to install libportaudio2"
    fi

    info "Installing Python dependencies..."
    "$PYTHON" -m pip install --quiet -r requirements.txt || die "pip install failed"
    ok "Dependencies installed."

    info "Downloading plugin model files (Silero VAD, etc.) ..."
    "$PYTHON" agent.py download-files || die "download-files failed"
    ok "Plugin files ready."
fi

# ── background process tracking ───────────────────────────────────────────
PIDS=()
mkdir -p logs

cleanup() {
    echo ""
    info "Stopping background services..."
    for pid in "${PIDS[@]:-}"; do
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    done
    ok "Done."
}
trap cleanup EXIT INT TERM

# ── 1. Ollama ──────────────────────────────────────────────────────────────
echo ""
info "Checking Ollama on :$OLLAMA_PORT ..."
if ! curl -sf --max-time 2 "http://localhost:$OLLAMA_PORT/api/tags" > /dev/null 2>&1; then
    die "Ollama is not running. Start it with:  ollama serve"
fi
ok "Ollama  →  http://localhost:$OLLAMA_PORT"

OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3.5:4b}"
ok "LLM model  →  $OLLAMA_MODEL"

# Whisper STT runs in-process (faster-whisper library) — no server needed.
info "Whisper STT will load model '$WHISPER_MODEL' in-process on first speech."

# ── 2. Agent (foreground) ──────────────────────────────────────────────────
echo ""
info "Launching agent in '$AGENT_MODE' mode..."
echo -e "${CYAN}────────────────────────────────────────────────${NC}"
"$PYTHON" agent.py "$AGENT_MODE"
