import os
import json
import asyncio
import logging
from fastapi import FastAPI, Request, HTTPException
from livekit import api as lk_api
from livekit.api import (
    CreateAgentDispatchRequest,
    RoomCompositeEgressRequest,
    EncodedFileOutput,
    S3Upload,
    EncodedFileType,
)
from dotenv import load_dotenv
import uvicorn

# Azure SDK
try:
    from azure.storage.blob import BlobServiceClient
    AZURE_AVAILABLE = True
except Exception:
    AZURE_AVAILABLE = False

load_dotenv()

# Setup logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("dispatcher")

# Name of the agent worker to dispatch into each new room. For this project the
# worker registers as "voice-agent" (see agent.py WorkerOptions.agent_name);
# override via DISPATCH_AGENT_NAME if you rename it.
DISPATCH_AGENT_NAME = os.getenv("DISPATCH_AGENT_NAME", "voice-agent")

# LiveKit signs every webhook: the Authorization header is a JWT (made with our
# API key/secret) that carries the body's sha256. Verifying it means only our own
# server can trigger agent dispatches and room teardowns.
_webhook_receiver = lk_api.WebhookReceiver(
    lk_api.TokenVerifier(os.getenv("LIVEKIT_API_KEY"), os.getenv("LIVEKIT_API_SECRET"))
)

app = FastAPI()

# keep track of rooms we've already dispatched (now keyed by room only)
dispatched = set()

# keep track of egress started per room to avoid duplicate egress starts
egress_started = set()

# Locks to prevent race conditions (concurrent webhooks)
dispatch_lock = asyncio.Lock()
egress_lock = asyncio.Lock()

from livekit.api import ListParticipantsRequest, RoomParticipantIdentity, DeleteRoomRequest
from livekit.api.twirp_client import TwirpError
from livekit.protocol.room import DeleteRoomRequest

async def cleanup_room(room: str):
    if not room:
        logger.error("cleanup_room called with no room")
        return

    lk = lk_api.LiveKitAPI(
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
        url=os.getenv("LIVEKIT_URL"),
    )

    try:
        logger.info(f"🔍 Listing participants for room: {room}")

        req = ListParticipantsRequest(room=room)
        resp = await lk.room.list_participants(req)

        participants = getattr(resp, "participants", [])
        logger.info(f"Found {len(participants)} participants to evaluate")

        removed_count = 0
        for p in participants:
            identity = getattr(p, "identity", "")

            # Protect sip_, vbt-, and agent-to identities from being kicked out during cleanup
            if identity.startswith(("sip_", "vbt-", "agent-to")):
                logger.debug(f"Skipping core/protected identity: {identity}")
                continue

            logger.info(f"🧹 Removing leftover: {identity}")
            remove_req = RoomParticipantIdentity(room=room, identity=identity)
            try:
                await lk.room.remove_participant(remove_req)
                removed_count += 1
                logger.debug(f"Successfully removed {identity}")
            except TwirpError as e:
                if e.code == "not_found":
                    logger.warning(f"Participant {identity} already gone, skipping removal")
                else:
                    logger.error(f"Failed to remove {identity}: {e}")
                    continue
            except Exception as e:
                logger.error(f"Unexpected error removing {identity}: {e}")
                continue

        logger.info(f"✨ Removed {removed_count} participants from room: {room}")

        # Gracefully close the room by deleting it (if it still exists)
        logger.info(f"🔒 Deleting room: {room}")
        try:
            delete_req = DeleteRoomRequest(room=room)
            await lk.room.delete_room(delete_req)
            logger.info(f"✅ Room {room} deleted successfully")
        except TwirpError as e:
            if e.code == "not_found":
                logger.info(f"Room {room} already deleted/not found, skipping")
            else:
                logger.error(f"Failed to delete room {room}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error deleting room {room}: {e}")

        dispatched.discard(room)
        egress_started.discard(room)

    except Exception as e:
        logger.exception(f"❌ Overall cleanup failed for room {room}: {e}")

    finally:
        await lk.aclose()


async def _poll_egress_and_upload_to_azure(lk: lk_api.LiveKitAPI, egress_id: str, local_path: str, container: str, account_name: str, account_key: str):
    """
    Polls LiveKit egress status until completion, then uploads the local file to Azure Blob Storage.
    """
    if not AZURE_AVAILABLE:
        logger.error("Azure SDK not installed; cannot upload to Azure. Install azure-storage-blob.")
        return

    logger.info("EGRESS-AZURE: start polling for egress_id=%s local_path=%s", egress_id, local_path)
    # poll for egress completion
    try:
        while True:
            try:
                # Accept several possible success states
                status = True
                if status and str(status).upper() in ("EGRESS_COMPLETE", "EGRESS_ENDED", "EGRESS_SUCCESS", "EGRESS_FINISHED"):
                    logger.info("EGRESS-AZURE: detected completion for egress_id=%s", egress_id)
                    break
                if status and str(status).upper() in ("EGRESS_FAILED", "FAILED", "ERROR"):
                    logger.warning("EGRESS-AZURE: egress reported failure for egress_id=%s: %s", egress_id, status)
                    return

            except Exception as e:
                logger.exception("EGRESS-AZURE: error while polling egress status: %s", e)

            await asyncio.sleep(3)

        # Wait until file exists (LiveKit may finish but file takes a moment)
        wait_seconds = 0
        while not os.path.exists(local_path) and wait_seconds < 60:
            await asyncio.sleep(1)
            wait_seconds += 1

        if not os.path.exists(local_path):
            logger.error("EGRESS-AZURE: expected file not found at %s after completion", local_path)
            return

        # Upload to Azure Blob
        blob_name = os.path.basename(local_path)
        try:
            account_url = f"https://{account_name}.blob.core.windows.net"
            service = BlobServiceClient(account_url=account_url, credential=account_key)
            container_client = service.get_container_client(container)
            # create container if not exists (idempotent)
            try:
                container_client.create_container()
            except Exception:
                pass

            blob_client = container_client.get_blob_client(blob_name)
            with open(local_path, "rb") as fh:
                blob_client.upload_blob(fh, overwrite=True)
            logger.info("EGRESS-AZURE: uploaded %s to container %s as blob %s", local_path, container, blob_name)
            # optionally remove local file
            try:
                os.remove(local_path)
                logger.debug("EGRESS-AZURE: removed local file %s", local_path)
            except Exception:
                logger.debug("EGRESS-AZURE: could not remove local file %s", local_path)
        except Exception as e:
            logger.exception("EGRESS-AZURE: failed to upload to Azure for %s: %s", local_path, e)

    except Exception as e:
        logger.exception("EGRESS-AZURE: unexpected error: %s", e)


async def _maybe_start_room_egress(lk: lk_api.LiveKitAPI, room: str):
    """
    Start room composite egress once per room. Supports both S3 and Azure.
    """
    azure_container = os.getenv("EGRESS_AZURE_CONTAINER")
    azure_account = os.getenv("EGRESS_AZURE_ACCOUNT_NAME")
    azure_key = os.getenv("EGRESS_AZURE_ACCOUNT_KEY")
    if azure_container and azure_account and azure_key:
        key = f"{room}"
        async with egress_lock:
            if key in egress_started:
                logger.debug("EGRESS-AZURE: already started for room %s, skipping.", room)
                return
            egress_started.add(key)

        # Use a local filepath for LiveKit to write into, then upload to Azure.
        filepath_template = os.getenv("EGRESS_FILEPATH", "{room_name}.ogg")
        local_dir = os.getenv("EGRESS_LOCAL_DIR", "./")
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, filepath_template.format(room_name=room))

        try:
            # create EncodedFileOutput without s3, pointing to local path
            file_output = EncodedFileOutput(
                file_type=EncodedFileType.OGG,
                filepath=local_path,
            )

            egress_req = RoomCompositeEgressRequest(
                room_name=room,
                audio_only=True,
                file_outputs=[file_output],
            )

            resp = await lk.egress.start_room_composite_egress(egress_req)
            logger.info("EGRESS-AZURE: started for room %s: %s", room, resp)

            # attempt to extract egress id from resp (robust)
            egress_id = None
            try:
                egress_id = getattr(resp, "egress_id", None) or getattr(resp, "id", None)
            except Exception:
                egress_id = None

            # if we have an egress id, poll and upload in background
            if egress_id:
                asyncio.create_task(_poll_egress_and_upload_to_azure(lk, egress_id, local_path, azure_container, azure_account, azure_key))
            else:
                # fallback: create a new LiveKit client dedicated for polling/uploading
                # (we close it immediately after use inside task)
                async def _bg():
                    try:
                        lk2 = lk_api.LiveKitAPI(
                            api_key=os.getenv("LIVEKIT_API_KEY"),
                            api_secret=os.getenv("LIVEKIT_API_SECRET"),
                            url=os.getenv("LIVEKIT_URL"),
                        )
                        # resp maybe contains some info; we can't poll without id but attempt gentle wait then upload if file appears
                        wait_seconds = 0
                        while wait_seconds < 120:
                            if os.path.exists(local_path):
                                # upload directly
                                await _poll_egress_and_upload_to_azure(lk2, None, local_path, azure_container, azure_account, azure_key)
                                break
                            await asyncio.sleep(2)
                            wait_seconds += 2
                    finally:
                        try:
                            await lk2.aclose()
                        except Exception:
                            pass

                asyncio.create_task(_bg())

            return
        except Exception as e:
            async with egress_lock:
                if key in egress_started:
                    egress_started.remove(key)
            logger.exception("EGRESS-AZURE: failed to start for room %s: %s", room, e)
            return

    # If neither S3 nor Azure configured, nothing to do
    logger.debug("EGRESS: skipping start - no upload backend configured (S3 or AZURE).")
    return


@app.post("/webhook")
async def on_event(req: Request):
    raw = (await req.body()).decode("utf-8", errors="replace")
    try:
        _webhook_receiver.receive(raw, req.headers.get("Authorization", ""))
    except Exception:
        logger.warning("Rejected webhook with a missing or invalid signature")
        raise HTTPException(401, "Invalid webhook signature")

    # log raw payload for debug purposes
    try:
        body = json.loads(raw)
    except Exception as e:
        logger.exception("Failed to parse JSON payload: %s", e)
        raise HTTPException(400, "Invalid JSON payload")

    logger.debug("WEBHOOK RECEIVED: %s", json.dumps(body))

    event_name = body.get("event")

    # handle participant leaving
    if event_name == "participant_left":
        part = body.get("participant", {})
        identity = part.get("identity")
        room = body.get("room", {}).get("name")

        logger.info(f"Participant LEFT: {identity} from {room}")

        # React when any core participant (sip_, vbt-, agent-to) exits
        if identity and identity.startswith(("sip_", "vbt-", "agent-to")):
            logger.info(f"Core participant left ({identity}) — cleaning room…")
            await cleanup_room(room)

        return {"ok": True, "event": "left"}

    if event_name == "egress_ended":
        egress = body.get("egress", {})
        file_outputs = egress.get("file_outputs", [])

        for f in file_outputs:
            local_path = f.get("filepath")
            if local_path:
                logger.info("EGRESS-AZURE: egress ended, uploading %s", local_path)
                asyncio.create_task(_upload_local_file_to_azure(local_path))

        return {"ok": True, "handled": "egress_ended"}
        
    if event_name != "participant_joined":
        logger.debug("Ignoring event: %s", event_name)
        return {"ok": True}

    part = body.get("participant", {})
    identity = part.get("identity")
    room = body.get("room", {}).get("name")

    # Expand whitelisted room prefixes to support sip-, vbt-, agent-to, and ws-
    if not room.startswith(("sip-", "vbt-", "agent-to", "ws-")):
        logger.debug("Skipping non-whitelisted room: %s", room)
        try:
            await cleanup_room(room)
            logger.info("Room destroyed: %s", room)
            return {"ok": True, "destroyed": True}
        except Exception as e:
            logger.exception("Failed to destroy room %s: %s", room, e)
        return {"ok": True, "skipped": "non_whitelisted_room"}

    logger.debug("Parsed room=%s identity=%s", room, identity)

    if not identity or not room:
        logger.warning("Malformed webhook, missing room or identity: %s", json.dumps(body))
        raise HTTPException(400, "Malformed webhook, missing room or identity")

    # Skip agent- identities EXCEPT when they explicitly match agent-to
    if identity.startswith("agent-") and not identity.startswith("agent-to"):
        logger.debug("Skipping agent identity: %s", identity)
        return {"ok": True, "skipped": "agent"}

    # Skip egress participants (identities like "EG_..." or "egress-...")
    if identity.startswith(("EG_", "EG-", "egress-", "egress_")):
        logger.debug("Skipping egress identity: %s", identity)
        return {"ok": True, "skipped": "egress"}

    # Stop multiple dispatching: ensure only one dispatch per room
    if identity == "transcriptions_backend_listener":
        logger.debug("Skipping participant: %s", identity)
        return {"ok": True, "skipped": "transcriptions_backend_listener"}

    key = f"{room}"
    logger.debug("Dispatched set before check: %s", dispatched)
    async with dispatch_lock:
        if key in dispatched:
            logger.debug("Already dispatched for room %s. Skipping.", room)
            return {"ok": True, "skipped": "already_dispatched"}
        # mark it so we won't do it again
        dispatched.add(key)
        logger.debug("Added to dispatched set: %s", key)

    # Pull metadata (if any)
    raw_meta = part.get("metadata", "")
    if raw_meta:
        try:
            meta_obj = json.loads(raw_meta)
        except json.JSONDecodeError:
            logger.warning("Invalid metadata JSON: %s", raw_meta)
            raise HTTPException(400, f"Invalid metadata JSON: {raw_meta}")
    else:
        meta_obj = {}

    # Extract participant attributes, including x-inin-cnv
    attrs = part.get("attributes", {})
    if attrs:
        meta_obj.update(attrs)  # Merge attributes into metadata
        logger.debug("Merged participant attributes into metadata: %s", attrs)
    else:
        logger.debug("No participant attributes found in webhook payload")

    # Build & fire your agent dispatch
    dispatch_req = CreateAgentDispatchRequest(
        agent_name=DISPATCH_AGENT_NAME,
        room=room,
        metadata=json.dumps(meta_obj),
    )

    logger.debug("Final metadata for dispatch: %s", json.dumps(meta_obj))

    # Create LiveKit client
    lk = lk_api.LiveKitAPI(
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
        url=os.getenv("LIVEKIT_URL"),
    )

    try:
        logger.debug("Creating agent dispatch for room=%s identity=%s", room, identity)
        # call create_dispatch and capture response
        try:
            resp = await lk.agent_dispatch.create_dispatch(dispatch_req)
            logger.info("Agent dispatch created: %s", resp)
        except Exception as e_dispatch:
            logger.exception("Agent dispatch failed: %s", e_dispatch)
            # Surface 500 only if dispatch failed (keeps prior behavior)
            # If dispatch fails, remove the dispatched marker so it can be retried
            async with dispatch_lock:
                if key in dispatched:
                    dispatched.remove(key)
                    logger.debug("Removed room from dispatched set due to dispatch failure: %s", key)
            raise HTTPException(500, f"Dispatch failed: {e_dispatch}")

        # Start egress but don't let egress failure affect response
        await _maybe_start_room_egress(lk, room)

    finally:
        await lk.aclose()
        logger.debug("Closed LiveKit client. dispatched=%s egress_started=%s", dispatched, egress_started)

    return {"dispatched_to": room, "identity": identity, "metadata": meta_obj}


if __name__ == "__main__":
    # Only the local LiveKit server posts here, so listen on localhost by default.
    uvicorn.run(app, host=os.getenv("DISPATCHER_HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "3001")), log_level="debug")
