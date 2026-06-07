# Personal Voice Assistant

A fully local, real-time voice assistant. No cloud APIs — speech-to-text, the LLM,
and text-to-speech all run on your own machine.

| Stage | Engine | Notes |
|-------|--------|-------|
| STT   | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | in-process, `base` model |
| LLM   | [Ollama](https://ollama.com) · `qwen3.5:4b` | native, GPU; thinking disabled via `/api/chat` → ~1s replies |
| TTS   | [Kokoro](https://github.com/thewh1teagle/kokoro-onnx) | in-process, ONNX |
| VAD   | Silero (bundled in `livekit-plugins-silero`) | |
| Orchestration | [LiveKit Agents](https://github.com/livekit/agents) 1.5 | console **or** room mode |

## Two ways to run

### 1. Native console (single machine, local mic/speakers)
Fastest path on the host itself.

```bash
ollama serve                 # in another terminal; pull qwen3.5:4b once
./start.sh --install         # first run: deps + model files + libportaudio2
./start.sh console           # talk via your mic/speakers
```

Sanity-check the pipeline without a mic anytime:

```bash
python smoke_test.py         # exercises LLM + TTS + STT (round-trip)
```

### 2. Docker LAN ecosystem (talk from any device's browser)
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

## Configuration

All config is via `.env` (see `.env.example` for the full list). Key variables:

| Variable | Purpose |
|----------|---------|
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | Shared LiveKit credentials (self-hosted) |
| `LIVEKIT_NODE_IP` | Host LAN IP advertised for WebRTC media |
| `LIVEKIT_WS_PUBLIC` | `wss://<LAN-IP>` the browser connects to |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` from containers |
| `OLLAMA_MODEL` / `WHISPER_MODEL` / `TTS_VOICE` | Model selection |

## Project layout

```
agent.py            LiveKit entrypoint (console + room mode)
session.py          Pipeline wiring (VAD/STT/LLM/TTS)
llm_ollama.py       Custom Ollama LLM via /api/chat (think:false → fast)
stt_whisper.py      In-process faster-whisper STT plugin
tts_kokoro.py       In-process Kokoro TTS plugin
tools.py            Function tools (empty placeholder for now)
token_server.py     Mints browser JWTs + dispatches the agent
web/index.html      Browser client (livekit-client)
Dockerfile.agent    Agent/token image with models baked in
docker-compose.yml  LiveKit + Redis + agent + token + Caddy
livekit.yaml        LiveKit server config
Caddyfile           TLS reverse proxy
smoke_test.py       Headless pipeline check
```

## License

Personal project.
