"""
Tiny token server for the web client.

Mints a LiveKit access token that (a) lets the browser join a room and
(b) explicitly dispatches the "voice-agent" worker into that room.

GET /token?room=<room>&identity=<identity>
  -> {"token": "<jwt>", "url": "<public wss url>"}

The agent uses explicit dispatch (agent_name="voice-agent" in agent.py), so the
dispatch instruction must travel in the token's room config — that is what
RoomConfiguration + RoomAgentDispatch below do.
"""

import os

from aiohttp import web
from livekit.api import (
    AccessToken,
    RoomAgentDispatch,
    RoomConfiguration,
    VideoGrants,
)

API_KEY = os.environ["LIVEKIT_API_KEY"]
API_SECRET = os.environ["LIVEKIT_API_SECRET"]
# The URL the *browser* uses to reach LiveKit (through Caddy), not the in-compose URL.
WS_PUBLIC = os.getenv("LIVEKIT_WS_PUBLIC", "wss://localhost")
AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "voice-agent")
PORT = int(os.getenv("TOKEN_PORT", "8080"))


def _cors(resp: web.Response) -> web.Response:
    # Same-origin behind Caddy makes this unnecessary, but it's a harmless safety net.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return resp


async def handle_token(request: web.Request) -> web.Response:
    room = request.query.get("room", "voice-room")
    identity = request.query.get("identity", "web-user")

    token = (
        AccessToken(API_KEY, API_SECRET)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(VideoGrants(room_join=True, room=room))
        .with_room_config(
            RoomConfiguration(
                agents=[RoomAgentDispatch(agent_name=AGENT_NAME)],
            )
        )
        .to_jwt()
    )
    return _cors(web.json_response({"token": token, "url": WS_PUBLIC}))


async def handle_options(request: web.Request) -> web.Response:
    return _cors(web.Response())


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def make_app() -> web.Application:
    app = web.Application()
    app.add_routes(
        [
            web.get("/token", handle_token),
            web.options("/token", handle_options),
            web.get("/health", handle_health),
        ]
    )
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
