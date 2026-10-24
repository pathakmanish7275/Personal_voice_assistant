#!/usr/bin/env bash
# Bring up the FULL self-hosted LiveKit stack using the local components under
# this repo (no LiveKit Cloud, no Docker image for the server):
#
#   livekit-server  (local binary)      :7880   signaling + WebRTC
#   dispatcher      (local_dispatcher)  :3001   webhook -> CreateAgentDispatch
#   web             (token_server)      :8080   mints tokens + serves index.html
#   agent           (agent.py dev)              this repo's voice-agent worker
#   monitor         (optional)                  resource monitor
#
# Flow: browser -> web(:8080) mints a ws-* room token -> joins livekit-server
# -> server fires participant_joined webhook -> dispatcher dispatches the
# "voice-agent" worker into the room -> agent runs Ollama + Whisper + Kokoro.
#
# Usage:
#   ./stack.sh                 # bring the whole stack up (Ctrl-C stops it)
#   ./stack.sh --monitor       # also open the resource monitor in this terminal
#   ./stack.sh --node-ip IP    # advertise a LAN IP so other devices can connect
#   ./stack.sh --down          # stop anything this script left running
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${CYAN}[info]${NC}  $*"; }
ok()   { echo -e "${GREEN}[ ok ]${NC}  $*"; }
warn() { echo -e "${YELLOW}[warn]${NC}  $*"; }
die()  { echo -e "${RED}[fail]${NC}  $*" >&2; exit 1; }

# Each component runs in its own process group (setsid), so stopping it means
# signalling the whole group — `agent.py dev` runs the real worker as a child of
# a watchfiles reloader, and killing just the parent PID orphaned that child.
PGID_FILE="$SCRIPT_DIR/logs/stack.pgids"
stop_groups() {  # stop_groups <pgid>... : TERM each group, KILL stragglers after 5s
    local pg i alive
    for pg in "$@"; do kill -TERM -- "-$pg" 2>/dev/null || true; done
    for ((i=0; i<50; i++)); do
        alive=false
        for pg in "$@"; do kill -0 -- "-$pg" 2>/dev/null && alive=true; done
        $alive || return 0
        sleep 0.1
    done
    for pg in "$@"; do kill -KILL -- "-$pg" 2>/dev/null || true; done
}

# ── args ─────────────────────────────────────────────────────────────────────
WITH_MONITOR=false
NODE_IP=""
for ((i=1; i<=$#; i++)); do
    arg="${!i}"
    case "$arg" in
        --down)
            info "Stopping self-hosted stack processes..."
            if [ -f "$PGID_FILE" ]; then
                # Newest first (agent before server) so nothing logs reconnect noise.
                mapfile -t groups < <(tac "$PGID_FILE")
                stop_groups "${groups[@]}"
                rm -f "$PGID_FILE"
                ok "stopped process groups from a previous run"
            fi
            # Fallback for anything started outside a recorded process group.
            pkill -f "livekit-server/bin/livekit-server" 2>/dev/null && ok "livekit-server stopped" || true
            pkill -f "livekit-dispatcher/local_dispatcher.py" 2>/dev/null && ok "dispatcher stopped" || true
            pkill -f "livekit-web/token_server.py" 2>/dev/null && ok "web token server stopped" || true
            pkill -f "agent.py dev" 2>/dev/null && ok "agent stopped" || true
            # Agent spawns multiprocessing forkserver children — clean those too.
            pkill -9 -f "forkserver.*agent.py" 2>/dev/null || true
            exit 0
            ;;
        --monitor) WITH_MONITOR=true ;;
        --node-ip) j=$((i+1)); NODE_IP="${!j:-}"; i=$j ;;
        *) die "Unknown argument: $arg" ;;
    esac
done

# ── canonical stack config ────────────────────────────────────────────────────
# Exported here so it WINS over each component's .env (load_dotenv does not
# override already-set env vars). This leaves the repo-root .env (which may
# point at LiveKit Cloud) untouched while this stack runs fully local.
#
# Credentials come from stack.env (gitignored; see stack.env.example). First
# run generates one with a fresh random key/secret.
STACK_ENV="$SCRIPT_DIR/stack.env"
if [ ! -f "$STACK_ENV" ]; then
    info "No stack.env found — generating one with a fresh random key/secret..."
    ( umask 077
      printf '# Local credentials for ./stack.sh — DO NOT COMMIT (see stack.env.example).\n' > "$STACK_ENV"
      python3 -c 'import secrets; print(f"LIVEKIT_API_KEY=API{secrets.token_hex(6)}\nLIVEKIT_API_SECRET={secrets.token_hex(24)}")' >> "$STACK_ENV"
    ) || die "Could not write $STACK_ENV"
    ok "Wrote stack.env"
fi
set -a; source "$STACK_ENV"; set +a
[ -n "${LIVEKIT_API_KEY:-}" ] && [ -n "${LIVEKIT_API_SECRET:-}" ] \
    || die "stack.env must set LIVEKIT_API_KEY and LIVEKIT_API_SECRET"
[[ "$LIVEKIT_API_SECRET" == change-me* ]] \
    && die "stack.env still has the placeholder secret — set a real one (or delete stack.env to regenerate)"
export LIVEKIT_URL="ws://localhost:7880"        # agent + dispatcher -> local server
export DISPATCH_AGENT_NAME="voice-agent"        # must match agent.py WorkerOptions

# URL the *browser* uses to reach the server. localhost for same machine; the
# LAN IP when --node-ip is given so phones/other laptops can connect.
if [ -n "$NODE_IP" ]; then
    export LIVEKIT_WS_URL="ws://${NODE_IP}:7880"
else
    export LIVEKIT_WS_URL="ws://localhost:7880"
fi

# Dispatcher + web ports (exported so they hold regardless of which .env loads).
export PORT="${PORT:-3001}"           # dispatcher webhook port (matches livekit.yaml)
export WEB_PORT="${WEB_PORT:-8080}"   # web token server / client port

# Agent providers: everything in-process/local (no STT/TTS containers needed).
export STT_PROVIDER="${STT_PROVIDER:-local}"
export TTS_PROVIDER="${TTS_PROVIDER:-local}"
export LLM_PROVIDER="${LLM_PROVIDER:-flm}"     # FLM on the NPU, Ollama as fallback
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
export FLM_BASE_URL="${FLM_BASE_URL:-http://localhost:52625}"
export FLM_MODEL="${FLM_MODEL:-qwen3.5:4b}"

# ── venv ───────────────────────────────────────────────────────────────────
[ -f ".venv/bin/activate" ] || die "No .venv found. Run ./start.sh --install first to create it."
source .venv/bin/activate
PY="$(command -v python)"

# Dispatcher + web token server need fastapi/uvicorn/livekit-api (already in the venv).
"$PY" -c "import fastapi, uvicorn, livekit.api" 2>/dev/null \
    || die "venv missing fastapi/uvicorn/livekit-api. Run: pip install fastapi uvicorn livekit-api"

# Semantic end-of-turn model: the agent only loads it from the local cache, so
# fetch it once (~500MB) if it's missing. Importing the plugin registers BOTH
# the English and multilingual runners, so every revision must be cached or the
# missing one fails at worker start. Without the plugin the agent falls back to
# pause-only endpointing.
if "$PY" -c "import livekit.plugins.turn_detector" 2>/dev/null \
   && ! "$PY" -c "
import os, sys
from livekit.plugins.turn_detector.models import MODEL_REVISIONS
refs = os.path.join(os.environ.get('HF_HOME', os.path.expanduser('~/.cache/huggingface')),
                    'hub', 'models--livekit--turn-detector', 'refs')
sys.exit(0 if all(os.path.exists(os.path.join(refs, r)) for r in MODEL_REVISIONS.values()) else 1)
" 2>/dev/null; then
    info "Downloading the end-of-turn model (one time)..."
    mkdir -p logs
    "$PY" agent.py download-files >logs/download-files.log 2>&1 \
        && ok "End-of-turn model downloaded" \
        || warn "End-of-turn model download failed (see logs/download-files.log)"
fi

# ── local server binary ──────────────────────────────────────────────────────
SERVER_BIN="$SCRIPT_DIR/livekit-server/bin/livekit-server"
[ -x "$SERVER_BIN" ] || die "LiveKit server binary not found/executable at $SERVER_BIN"
ok "LiveKit server binary: $("$SERVER_BIN" --version 2>&1 | head -1)"

# ── Ollama check (the agent's LLM) ────────────────────────────────────────────
if ! curl -sf --max-time 2 "${OLLAMA_BASE_URL}/api/tags" >/dev/null 2>&1; then
    if [ "$LLM_PROVIDER" = "flm" ]; then
        warn "Ollama not reachable at ${OLLAMA_BASE_URL} — no fallback if FLM is busy or down."
    else
        warn "Ollama not reachable at ${OLLAMA_BASE_URL} — the agent will fail to answer."
    fi
    warn "Start it with:  ollama serve   (and pull your model, e.g. ollama pull qwen3.5:4b)"
else
    ok "Ollama → ${OLLAMA_BASE_URL}"
fi

# ── FLM on the NPU (primary LLM; Ollama is the fallback) ─────────────────────
# Started as the supervised `flm-npu` user unit (same as Personal-Database), and
# deliberately NOT stopped with the stack: it may be serving the Personal-Database
# too, and FLM must never be killed mid-generation. Stop it yourself with
#   systemctl --user stop flm-npu
if [ "$LLM_PROVIDER" = "flm" ]; then
    FLM_PORT="${FLM_BASE_URL##*:}"
    if curl -sf --max-time 2 "${FLM_BASE_URL}/api/tags" >/dev/null 2>&1; then
        ok "FLM → ${FLM_BASE_URL} (already running)"
    elif ! command -v flm >/dev/null 2>&1; then
        warn "FLM not installed — every turn will use the Ollama fallback."
    else
        info "Starting FLM (${FLM_MODEL}) on the NPU as user unit flm-npu..."
        systemctl --user reset-failed flm-npu 2>/dev/null || true
        if systemd-run --user --unit=flm-npu --property=LimitMEMLOCK=infinity \
               flm serve "$FLM_MODEL" --port "$FLM_PORT" -q 1 -s 4 >/dev/null 2>&1; then
            for _ in $(seq 1 30); do
                curl -sf --max-time 2 "${FLM_BASE_URL}/api/tags" >/dev/null 2>&1 && break
                sleep 1
            done
        fi
        if curl -sf --max-time 2 "${FLM_BASE_URL}/api/tags" >/dev/null 2>&1; then
            ok "FLM → ${FLM_BASE_URL} (started; model loads on first request)"
        else
            warn "FLM didn't come up (see: journalctl --user -u flm-npu) — using the Ollama fallback."
        fi
    fi
    FLM_PKG="$(dpkg-query -W -f='${Version}' fastflowlm 2>/dev/null || true)"
    case "$FLM_PKG" in
        *slotfix*) ok "FLM package ${FLM_PKG}: interrupted answers are dropped (NPU freed at once)" ;;
        "")        ;;
        *)         info "FLM package ${FLM_PKG} (stock): interrupted answers are drained, not dropped — see tools/flm-slotfix" ;;
    esac
fi

# ── process tracking ──────────────────────────────────────────────────────────
mkdir -p logs
PGIDS=()
: > "$PGID_FILE"
start_group() {  # start_group <logfile> <cmd...> : run cmd in its own process group
    local log="$1"; shift
    setsid "$@" >"$log" 2>&1 &
    PGIDS+=($!)              # setsid execs in place, so $! is the new group id
    echo "$!" >> "$PGID_FILE"
}
FG_PID=""   # the log tail / monitor we wait on at the end
cleanup() {
    echo ""
    info "Shutting down stack..."
    [ -n "$FG_PID" ] && kill "$FG_PID" 2>/dev/null
    local rev=() i
    for ((i=${#PGIDS[@]}-1; i>=0; i--)); do rev+=("${PGIDS[i]}"); done
    [ ${#rev[@]} -gt 0 ] && stop_groups "${rev[@]}"
    rm -f "$PGID_FILE"
    ok "Stopped."
}
# INT/TERM just exit; the EXIT trap then runs cleanup exactly once.
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wait_http() {  # wait_http <url> <label> [timeout_s]
    local url="$1" label="$2" timeout="${3:-20}" i=0
    while (( i < timeout )); do
        if curl -s -o /dev/null --max-time 2 "$url"; then ok "$label is up"; return 0; fi
        sleep 1; ((i++))
    done
    die "$label did not come up at $url within ${timeout}s (see logs/)"
}

# ── 1. LiveKit server (local binary) ──────────────────────────────────────────
echo ""
# Base config + a webhook section generated from stack.env/PORT, so the webhook
# signing key always matches. The secret itself goes in via LIVEKIT_KEYS (env,
# not argv), so neither file on disk holds it.
RUNTIME_CONF="$SCRIPT_DIR/livekit-server/livekit.runtime.yaml"
{
    cat "$SCRIPT_DIR/livekit-server/livekit.yaml"
    printf '\n# --- generated by stack.sh ---\nwebhook:\n  api_key: %s\n  urls:\n    - http://localhost:%s/webhook\n' \
        "$LIVEKIT_API_KEY" "$PORT"
} > "$RUNTIME_CONF"
SERVER_ARGS=(--config "$RUNTIME_CONF")
[ -n "$NODE_IP" ] && SERVER_ARGS+=(--node-ip "$NODE_IP")
info "Starting livekit-server ${NODE_IP:+(node-ip $NODE_IP) }..."
export LIVEKIT_KEYS="$LIVEKIT_API_KEY: $LIVEKIT_API_SECRET"
start_group logs/livekit-server.log "$SERVER_BIN" "${SERVER_ARGS[@]}"
unset LIVEKIT_KEYS
wait_http "http://localhost:7880/" "livekit-server (:7880)" 20

# ── 2. Dispatcher (webhook -> agent dispatch) ─────────────────────────────────
# Launched by path from the repo root (not cd) so the process cmdline stays
# distinctive — that's what --down and the monitor match on. Config comes from
# the env exported above, which overrides any .env these scripts load.
info "Starting dispatcher (:$PORT)..."
start_group logs/dispatcher.log "$PY" livekit-dispatcher/local_dispatcher.py
wait_http "http://localhost:$PORT/openapi.json" "dispatcher (:$PORT)" 20

# ── 3. Web token server + client ──────────────────────────────────────────────
info "Starting web token server (:$WEB_PORT)..."
start_group logs/web.log "$PY" livekit-web/token_server.py
wait_http "http://localhost:$WEB_PORT/" "web (:$WEB_PORT)" 20

# ── 4. Agent worker (this repo's voice-agent) ─────────────────────────────────
info "Starting agent worker (voice-agent, dev mode)..."
start_group logs/agent.log "$PY" agent.py dev
sleep 2
ok "Agent worker launched (registering with the server for explicit dispatch)"

echo ""
ok "Self-hosted stack is up."
echo -e "   ${CYAN}Web client:${NC}  http://${NODE_IP:-localhost}:8080"
echo -e "   ${CYAN}Logs:${NC}        logs/{livekit-server,dispatcher,web,agent}.log"
echo ""

# ── 5. Monitor (optional) or agent log tail, until Ctrl-C / SIGTERM ───────────
# Run in the background and `wait` on it: bash defers trapped signals until a
# foreground command returns, so a foreground `tail -f` made SIGTERM hang.
if [ "$WITH_MONITOR" = true ]; then
    "$PY" -c "import psutil" 2>/dev/null || { info "Installing psutil for the monitor..."; "$PY" -m pip install --quiet psutil; }
    info "Launching monitor (Ctrl-C stops the whole stack)..."
    ( cd "$SCRIPT_DIR/livekit-monitor" && exec "$PY" monitor.py ) &
else
    info "Tailing agent log. Ctrl-C stops the whole stack."
    echo -e "${CYAN}────────────────────────────────────────────────${NC}"
    tail -f logs/agent.log &
fi
FG_PID=$!
wait "$FG_PID"
