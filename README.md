# Personal Voice Assistant

A fully local, real-time voice assistant. No cloud APIs — speech-to-text, the LLM,
and text-to-speech all run on your own machine.

| Stage | Engine | Notes |
|-------|--------|-------|
| STT   | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | in-process, `base` model |
| LLM   | [FastFlowLM](https://github.com/ROCm/FastFlowLM) · `qwen3.5:4b` on the AMD NPU, [Ollama](https://ollama.com) fallback | FLM via `/v1/chat/completions` (~2.5s first token); Ollama `/api/chat`, think off, when the NPU is busy/down |
| TTS   | [Kokoro](https://github.com/thewh1teagle/kokoro-onnx) | in-process, ONNX |
| VAD   | Silero (bundled in `livekit-plugins-silero`) | |
| End of turn | [LiveKit turn detector](https://github.com/livekit/agents/tree/main/livekit-plugins/livekit-plugins-turn-detector) (multilingual, local ONNX) | waits through unfinished phrases instead of answering half a thought |
| Orchestration | [LiveKit Agents](https://github.com/livekit/agents) 1.5 | console **or** room mode |

**Hardware:** the LLM runs on an AMD Ryzen AI (XDNA2) NPU through FastFlowLM on
Linux. Without one, nothing breaks — every turn simply falls back to Ollama
(CPU/GPU), just slower to first word (~7 s on a CPU-only machine).

## Modular providers

STT, LLM, and TTS are **swappable providers**, the same way LiveKit treats its
cloud plugins. Each is chosen by env var; the default is fully local/in-process:

| Stage | `*_PROVIDER=local`/`ollama` (default) | `*_PROVIDER=openai` |
|-------|----------------------------------------|----------------------|
| STT   | in-process faster-whisper              | `openai.STT(base_url=…)` → local Docker server **or** OpenAI cloud |
| LLM   | FLM (NPU) → Ollama fallback (`LLM_PROVIDER=flm`, default) · Ollama only (`ollama`) | `openai.LLM(base_url=…)` → any OpenAI-compatible LLM |
| TTS   | in-process Kokoro                      | `openai.TTS(base_url=…)` → local Docker server **or** OpenAI cloud |

Because the local Docker STT/TTS servers speak the OpenAI audio API, switching
between **local Docker** and **the cloud** is just a change of `base_url` — no code
changes. See `session.py` (`build_stt` / `build_llm` / `build_tts`) and `.env.example`.

## Ways to run

### 1. Native console (single machine, local mic/speakers)
Fastest path on the host itself — everything in-process.

```bash
ollama serve                 # in another terminal; pull qwen3.5:4b once (the fallback LLM)
./start.sh --install         # first run: deps + model files + libportaudio2
./start.sh console           # talk via your mic/speakers
```

Sanity-check the pipeline without a mic anytime:

```bash
python smoke_test.py         # exercises LLM + TTS + STT (round-trip)
```

### 2. STT + TTS in Docker, agent native (one-click experiment)
Runs Whisper and Kokoro as containerized OpenAI-compatible providers while the
agent and Ollama stay native — so heavy deps/models live in Docker but you can
edit and re-run agent code instantly. No LiveKit/browser involved.

```bash
OLLAMA_HOST=0.0.0.0 ollama serve     # native, in another terminal
./experiment.sh                      # builds STT/TTS containers, then talk via mic
./experiment.sh --agent-only         # skip Docker; relaunch agent against running containers
./experiment.sh --down               # stop the STT/TTS containers when done
```

`--agent-only` is handy for fast iteration (tweak agent code, relaunch without
touching containers) and when your shell lacks docker-group access but the
containers are already up.

Under the hood `experiment.sh` brings up `docker-compose.dev.yml` (the `stt` and
`tts` services on ports 8001/8002), waits for health, then launches the agent in
console mode with `STT_PROVIDER=openai` / `TTS_PROVIDER=openai` pointed at them.
Swap either to the cloud later by changing the `*_BASE_URL` / `*_API_KEY`.

### 3. Docker LAN ecosystem (talk from any device's browser)
Self-hosts the full LiveKit stack on one host; phones/laptops on the same network
connect over the browser. Console mode can't do this because Docker can't reach
host mic/speakers on macOS/Windows — so we use **room mode**, where the browser
owns the mic.

```
 Browser (Mac/Win/phone) ── https://<LAN-IP> ──┐
                                                │  (mic in the tab)
   Caddy :443  ── TLS, serves web + proxies /token and /rtc
        │
   LiveKit server ←→ Redis        Token server (JWT + agent dispatch)
        │ dispatches "voice-agent"
   Agent (headless, Whisper+Kokoro baked in)
        │ host.docker.internal:11434
   Ollama (native on host, GPU)
```

#### Prerequisites
- **Docker + Docker Compose** on the host (Docker Desktop on macOS/Windows).
- **Ollama running natively**, reachable from containers:
  ```bash
  OLLAMA_HOST=0.0.0.0 ollama serve      # must bind 0.0.0.0, not just 127.0.0.1
  ollama pull qwen3.5:4b
  ```
- A copy of the config: `cp .env.example .env`, then set **`LIVEKIT_NODE_IP`** and
  the host in **`LIVEKIT_WS_PUBLIC`** to this machine's LAN IP
  (`hostname -I` / `ipconfig getifaddr en0` / `ipconfig`).
- Open the host firewall for `443/tcp`, `7881/tcp`, and `50000-50100/udp`.

#### Run
```bash
docker compose build
docker compose up
```

Then from any device on the network open **`https://<LAN-IP>`**, accept the
self-signed certificate warning once, pick a room, and click **Connect & talk**.

> **Why HTTPS / the cert warning?** Browsers only allow microphone access on a
> secure origin. The stack is served over HTTPS by Caddy using a self-signed
> (internal CA) certificate, so each device shows a one-time "not private" warning
> you can safely accept on your own LAN. Media itself (WebRTC) is always encrypted.

### 4. Self-hosted LiveKit **binary** stack (no Docker image, webhook dispatch)
Runs the actual LiveKit components vendored under `livekit-*/` — the server runs
from the **prebuilt binary** (not the `livekit/livekit-server` Docker image or
LiveKit Cloud), and a **webhook dispatcher** auto-dispatches the agent instead of
the token embedding the dispatch. One script brings the whole thing up:

```bash
ollama serve                 # native, in another terminal; pull qwen3.5:4b once (fallback LLM)
./stack.sh                   # FLM (if needed) + server + dispatcher + web + agent (Ctrl-C stops all)
./stack.sh --monitor         # same, plus the live resource monitor
./stack.sh --node-ip <LAN-IP>  # advertise a LAN IP so other devices can join
./stack.sh --down            # stop anything the script left running
```

Then open **`http://localhost:8080`** and click **Start talking**. Flow:

```
 Browser ── :8080 web/token_server ──► mints a ws-* room token (url = local server)
        └─► joins livekit-server (local binary, :7880)
                 │ participant_joined webhook
                 ▼
            dispatcher (:3001)  ──► CreateAgentDispatch(agent_name="voice-agent")
                 ▼
            agent.py (dev worker) ──► Whisper ─► FLM on the NPU (Ollama fallback) ─► Kokoro
```

On first run `stack.sh` also downloads the end-of-turn model (~500 MB, once) and,
if FastFlowLM is installed but not serving, starts it as the `flm-npu` user
service. It **never stops FLM** on teardown — other apps may share it; stop it
with `systemctl --user stop flm-npu`.

**The web client** (`livekit-web/index.html`) shows one glowing orb that follows
the agent's real state (listening / thinking / speaking), live captions, an
optional transcript (**T**), mute with a level meter (**M**), and a pill showing
which LLM answered (`FLM · NPU` or `Ollama · fallback`). Light/dark, phone-width.
The LiveKit client library is vendored under `livekit-web/static/`, so the page
works offline.

> Browsers only grant the microphone on a secure origin. `http://localhost:8080`
> counts as one, so same-machine use just works; other devices on the LAN need
> HTTPS in front (e.g. the `Caddyfile` from option 3).

**Credentials** live in `stack.env` (gitignored; template: `stack.env.example`).
The first `./stack.sh` run generates it with a fresh random key/secret. No
committed file holds a secret: the server gets it via the `LIVEKIT_KEYS` env var,
and `stack.sh` renders `livekit-server/livekit.runtime.yaml` (base config + webhook
section) at launch. To run the dispatcher or web server standalone, copy their
`.env.example` to `.env`.

`stack.sh` exports the local key/URL at launch, so they **override** each
component's `.env` and the repo-root `.env` (which may point at LiveKit Cloud) —
nothing is edited, the stack just runs fully local for its lifetime. The server
runs single-node (no Redis). Recording/egress stays off unless you set the
`EGRESS_*` vars the dispatcher reads.

> Note: this is a different design from option 3. Option 3 uses the LiveKit
> *Docker image* + Caddy TLS + token-embedded dispatch; option 4 uses the local
> *binary* + a webhook dispatcher. Don't run both on `:7880`/`:8080` at once.

## The LLM on the NPU (FastFlowLM)

`LLM_PROVIDER=flm` (the default) talks to FastFlowLM's OpenAI-format endpoint
(`llm_flm.py`). Things it handles that a plain OpenAI client wouldn't:

- **One request on the NPU at a time,** across all call processes (a file lock).
  If the NPU stays busy longer than `FLM_LOCK_WAIT`, the turn goes to Ollama.
- **Falls back to Ollama** when FLM is down, errors, or produces nothing in time.
- **Interrupted answers.** When you talk over the agent, its unfinished FLM answer
  is abandoned. Stock `flm serve` leaks a connection slot every time a client
  drops a stream ([ROCm/FastFlowLM#680](https://github.com/ROCm/FastFlowLM/issues/680)),
  so by default the agent reads the abandoned answer to the end instead (safe,
  but the next turn waits). With the patched package from
  [`tools/flm-slotfix/`](tools/flm-slotfix/), it drops the stream and the NPU is
  free in milliseconds. This is detected automatically from the installed package.

To build and install the patched FLM (rebuilds an official release inside FLM's
own build container; see [`tools/flm-slotfix/README.md`](tools/flm-slotfix/README.md)):

```bash
tools/flm-slotfix/build-deb.sh v1.0.10
systemctl --user stop flm-npu
sudo dpkg -i tools/flm-slotfix/dist/fastflowlm_1.0.10+slotfix1_amd64.deb
sudo apt-mark hold fastflowlm
```

## Turn-taking

A voice agent must not answer half a thought. The agent uses LiveKit's local
**semantic end-of-turn model**: after a pause it scores the transcript, so
"…so that multiple people can" keeps it waiting while a finished question gets
a reply after the usual short pause. **Preemptive generation** (starting an
answer before the turn is confirmed) is off when the LLM is FLM, since a
discarded early answer would occupy the NPU.

## Knowledge base (optional)

The agent's one tool, `search_my_work`, queries a local search service
(`SEARCH_URL`, default `http://127.0.0.1:8766/search`) over your own notes. Nothing
in this repo starts that service; when it isn't running, the agent says it can't
reach your notes and everything else works normally.

## Configuration

All config is via `.env` (see `.env.example` for the full list). Key variables:

| Variable | Purpose |
|----------|---------|
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | Shared LiveKit credentials (self-hosted). For `./stack.sh` they live in `stack.env` instead (auto-generated) |
| `LIVEKIT_NODE_IP` | Host LAN IP advertised for WebRTC media |
| `LIVEKIT_WS_PUBLIC` | `wss://<LAN-IP>` the browser connects to |
| `LLM_PROVIDER` | `flm` (default: NPU, Ollama fallback), `ollama`, or `openai` |
| `FLM_BASE_URL` / `FLM_MODEL` | FastFlowLM server (default `http://localhost:52625`, `qwen3.5:4b`) |
| `FLM_MAX_TOKENS` / `FLM_LOCK_WAIT` | Cap per reply (400); seconds to wait for a busy NPU before falling back (4) |
| `FLM_ABORT_ON_CANCEL` | `auto` (default: drop interrupted answers only on a `+slotfix` FLM), `1`, or `0` |
| `OLLAMA_BASE_URL` | Fallback LLM; `http://host.docker.internal:11434` from containers |
| `OLLAMA_MODEL` / `WHISPER_MODEL` / `TTS_VOICE` | Model selection |
| `SEARCH_URL` | Knowledge-base search service for `search_my_work` (optional) |

## Project layout

```
agent.py              LiveKit entrypoint (console + room mode)
session.py            Provider factory + pipeline wiring (VAD/STT/LLM/TTS)
llm_flm.py            FLM (NPU) LLM: /v1 + tools, cross-process NPU lock, drain/drop on interrupt, Ollama fallback
prompts/system_prompt.md  The agent's persona and the facts it may state about itself
llm_ollama.py         Custom Ollama LLM via /api/chat (think:false → fast)
stt_whisper.py        In-process faster-whisper STT plugin (provider: local)
tts_kokoro.py         In-process Kokoro TTS plugin (provider: local)
stt_server.py         OpenAI-compatible Whisper server (provider: openai/docker)
tts_server.py         OpenAI-compatible Kokoro server (provider: openai/docker)
tools.py              Function tools (search_my_work: local RAG over the user's notes)
token_server.py       Mints browser JWTs + dispatches the agent
web/index.html        Browser client (livekit-client)
experiment.sh         One-click: STT/TTS in Docker + native agent console
start.sh              Native runner (console/dev/start)
stack.sh              One-click self-hosted binary stack (option 4)
stack.env.example     Template for stack.env (LiveKit key/secret; generated on first run)
livekit-server/       Vendored LiveKit server source + prebuilt binary (bin/)
livekit-dispatcher/   Webhook dispatcher — CreateAgentDispatch on participant_joined
livekit-web/          Standalone token server + browser client (static/: vendored livekit-client)
livekit-monitor/      psutil resource monitor for the running stack
tools/flm-slotfix/    Builds FastFlowLM with the connection-slot leak fixed (.deb)
Dockerfile.agent      Agent/token image with models baked in
Dockerfile.services   STT/TTS server image with models baked in
docker-compose.yml    Full LAN stack: LiveKit + Redis + agent + token + Caddy
docker-compose.dev.yml  Experiment stack: just the STT + TTS services
livekit.yaml          LiveKit server config
Caddyfile             TLS reverse proxy
smoke_test.py         Headless pipeline check
```

## License

Released under the [MIT License](LICENSE).
