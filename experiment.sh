#!/usr/bin/env bash
# One-click single-machine experiment setup:
#   - STT + TTS run in Docker (OpenAI-compatible providers)
#   - Ollama runs natively on the host
#   - the agent runs natively in console mode (your mic/speakers)
#
# Usage: ./experiment.sh            # bring up STT/TTS, then talk
#        ./experiment.sh --down     # stop the STT/TTS containers
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${CYAN}[info]${NC}  $*"; }
ok()   { echo -e "${GREEN}[ ok ]${NC}  $*"; }
warn() { echo -e "${YELLOW}[warn]${NC}  $*"; }
die()  { echo -e "${RED}[fail]${NC}  $*" >&2; exit 1; }

COMPOSE="docker compose -f docker-compose.dev.yml"

if [ "${1:-}" = "--down" ]; then
    info "Stopping STT/TTS containers..."
    $COMPOSE down
    ok "Stopped."
    exit 0
fi

command -v docker >/dev/null || die "Docker not found. Install Docker, then re-run."

# ── venv ─────────────────────────────────────────────────────────────────────
if [ ! -f ".venv/bin/activate" ]; then
    info "Creating virtualenv..."
    python3 -m venv .venv || die "venv creation failed"
    ./.venv/bin/pip install --quiet -r requirements.txt || die "pip install failed"
fi
source .venv/bin/activate

# ── Ollama (native) ──────────────────────────────────────────────────────────
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
info "Checking native Ollama on :$OLLAMA_PORT ..."
curl -sf --max-time 2 "http://localhost:$OLLAMA_PORT/api/tags" >/dev/null 2>&1 \
    || die "Ollama not running. Start it with:  ollama serve   (and: ollama pull ${OLLAMA_MODEL:-qwen3.5:4b})"
ok "Ollama → http://localhost:$OLLAMA_PORT"

# ── STT/TTS containers ───────────────────────────────────────────────────────
info "Building & starting STT/TTS containers (first run downloads ~480MB of models)..."
$COMPOSE up -d --build || die "docker compose failed"

info "Waiting for STT (:8001) and TTS (:8002) to be ready..."
curl -s --retry 30 --retry-connrefused --retry-delay 1 --max-time 90 http://localhost:8001/health >/dev/null \
    || die "STT server did not become healthy"
curl -s --retry 30 --retry-connrefused --retry-delay 1 --max-time 90 http://localhost:8002/health >/dev/null \
    || die "TTS server did not become healthy"
ok "STT → http://localhost:8001   TTS → http://localhost:8002"

# ── Run the agent natively, pointed at the Docker providers ───────────────────
# Exported vars take precedence over .env (load_dotenv does not override env).
export STT_PROVIDER=openai STT_BASE_URL=http://localhost:8001/v1 STT_MODEL=whisper STT_API_KEY=local
export TTS_PROVIDER=openai TTS_BASE_URL=http://localhost:8002/v1 TTS_MODEL=tts-1 TTS_API_KEY=local
export TTS_VOICE="${TTS_VOICE:-af_heart}"
export LLM_PROVIDER=ollama
export OLLAMA_BASE_URL="http://localhost:${OLLAMA_PORT}"

echo ""
ok "Providers → STT: docker/whisper  |  LLM: native/ollama  |  TTS: docker/kokoro"
info "Launching agent in console mode (talk via your mic). Ctrl-C to stop."
echo -e "${CYAN}────────────────────────────────────────────────${NC}"
python agent.py console

echo ""
info "Agent stopped. STT/TTS containers are still running."
info "Stop them with:  ./experiment.sh --down"
