import os
import argparse
import datetime
from livekit.api import AccessToken, VideoGrants
from dotenv import load_dotenv

load_dotenv()

parser = argparse.ArgumentParser(description="Generate a LiveKit join token")
parser.add_argument("--room",     default="my-room",   help="Room name (default: my-room)")
parser.add_argument("--identity", default="test-user", help="Participant identity (default: test-user)")
parser.add_argument("--ttl",      type=int, default=3600, help="Token TTL in seconds (default: 3600)")
args = parser.parse_args()

api_key    = os.getenv("LIVEKIT_API_KEY")
api_secret = os.getenv("LIVEKIT_API_SECRET")
lk_url     = os.getenv("LIVEKIT_URL")

if not api_key or not api_secret:
    raise SystemExit("ERROR: LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set in .env")

token = (
    AccessToken(api_key, api_secret)
    .with_grants(VideoGrants(room_join=True, room=args.room))
    .with_identity(args.identity)
    .with_ttl(datetime.timedelta(seconds=args.ttl))
    .to_jwt()
)

print(f"Room:     {args.room}")
print(f"Identity: {args.identity}")
print(f"TTL:      {args.ttl}s")
print(f"URL:      {lk_url}")
print(f"\nToken:\n{token}")
