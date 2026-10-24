import os
import json
import datetime
import uuid

from fastapi import FastAPI
from fastapi.responses import FileResponse
from livekit.api import AccessToken, VideoGrants
from dotenv import load_dotenv
import uvicorn

load_dotenv()

API_KEY = os.getenv("LIVEKIT_API_KEY")
API_SECRET = os.getenv("LIVEKIT_API_SECRET")
# URL the *browser* uses to reach the LiveKit server (ws:// or wss://)
LIVEKIT_URL = os.getenv("LIVEKIT_WS_URL", os.getenv("LIVEKIT_URL", "ws://localhost:7880"))

if not API_KEY or not API_SECRET:
    raise SystemExit("ERROR: LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set (.env)")

HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI()


@app.get("/token")
def token(
    room: str | None = None,
    identity: str | None = None,
    # Fields agent.py reads from the job metadata (forwarded by the dispatcher).
    name: str | None = None,
    mobile_number: str | None = None,
    language: str | None = None,
):
    # Rooms must start with "ws-" so the dispatcher auto-dispatches the agent.
    room = room or f"ws-{uuid.uuid4().hex[:8]}"
    if not room.startswith("ws-"):
        room = f"ws-{room}"
    identity = identity or f"web-{uuid.uuid4().hex[:6]}"

    # Build participant metadata. The dispatcher forwards this into the agent
    # dispatch (local_dispatcher.py), and agent.py reads name / mobile_number /
    # language from it.
    meta: dict[str, str] = {}
    if name:
        meta["name"] = name.strip()
    if mobile_number:
        meta["mobile_number"] = mobile_number.strip()
    if language:
        meta["language"] = language.strip()

    builder = (
        AccessToken(API_KEY, API_SECRET)
        .with_identity(identity)
        .with_grants(VideoGrants(room_join=True, room=room))
        .with_ttl(datetime.timedelta(hours=1))
    )
    if meta:
        builder = builder.with_metadata(json.dumps(meta))

    return {
        "token": builder.to_jwt(),
        "url": LIVEKIT_URL,
        "room": room,
        "identity": identity,
        "metadata": meta,
    }


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "index.html"))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("WEB_PORT", "8080")))
