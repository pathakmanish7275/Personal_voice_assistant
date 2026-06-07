import logging
import os
import urllib.parse

import aiohttp
from livekit.agents import RunContext, function_tool

logger = logging.getLogger("tools")

# Local vector-search endpoint over the user's own work/notes (RAG).
SEARCH_URL = os.getenv("SEARCH_URL", "http://127.0.0.1:8766/search")
SEARCH_MODE = os.getenv("SEARCH_MODE", "vector")
SEARCH_TOP_K = int(os.getenv("SEARCH_TOP_K", "4"))
SEARCH_CHUNK_CHARS = int(os.getenv("SEARCH_CHUNK_CHARS", "600"))
SEARCH_TIMEOUT = float(os.getenv("SEARCH_TIMEOUT", "10"))


@function_tool
async def search_my_work(context: RunContext, query: str) -> str:
    """Search the user's personal work knowledge base (their notes, projects, code,
    and architecture/design decisions) and return the most relevant excerpts.

    Use this whenever the user asks about THEIR OWN work — their projects, repos,
    services, decisions, or notes — rather than general knowledge. Summarize the
    results in your own words for the voice reply; do not read them verbatim.

    Args:
        query: A concise natural-language search query describing what to look for.
    """
    url = f"{SEARCH_URL}?{urllib.parse.urlencode({'q': query, 'mode': SEARCH_MODE})}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=SEARCH_TIMEOUT)
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
    except Exception as e:
        logger.warning("search_my_work failed: %s", e)
        return "I couldn't reach your knowledge base just now."

    chunks = data.get("chunks", []) or []
    if not chunks:
        return f"I didn't find anything in your notes about '{query}'."

    parts = []
    for c in chunks[:SEARCH_TOP_K]:
        text = " ".join((c.get("text") or "").split())
        if len(text) > SEARCH_CHUNK_CHARS:
            text = text[:SEARCH_CHUNK_CHARS] + "…"
        source = c.get("name") or "unknown source"
        parts.append(f"[{source}] {text}")

    logger.info("search_my_work(%r) -> %d chunks", query, len(parts))
    return "\n\n".join(parts)


def get_tools() -> list:
    return [search_my_work]
