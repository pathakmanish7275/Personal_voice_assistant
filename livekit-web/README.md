# LiveKit Web — lightweight voice-bot front-end

A single vanilla HTML page (no build step) plus a tiny FastAPI token server.
The LiveKit browser client is vendored in `static/` (pinned to 2.16.1 — newer
clients probe a signal path the bundled livekit-server 1.9.9 doesn't serve), so
the page works offline. Joins a `ws-` room so the dispatcher auto-dispatches
`voice-agent`. Normally started by `../stack.sh`.

## Run

```bash
cd livekit-web
pip install fastapi uvicorn livekit-api python-dotenv   # already present if you ran the dispatcher
python token_server.py
```

Open http://localhost:8080 , click **Start talking**, and allow the microphone.

## How it fits your infra

1. Browser asks `/token` → server mints a JWT for a `ws-xxxx` room.
2. Browser joins the room. LiveKit server fires `participant_joined` → dispatcher
   (`:3001/webhook`) sees the `ws-` prefix and dispatches `voice-agent`.
3. Agent joins, you talk; agent audio + live transcriptions render on the page.

## Notes

- `LIVEKIT_WS_URL` must be reachable **from the browser**. `ws://localhost:7880`
  is fine on the same machine; for other devices use the host LAN IP or a `wss://`
  domain (browsers block `ws://` on https pages).
- Requires the LiveKit server, the dispatcher, and the agent worker to be running.
