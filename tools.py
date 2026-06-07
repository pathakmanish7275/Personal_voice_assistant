import logging

from livekit.agents import RunContext, function_tool

logger = logging.getLogger("tools")

# No active tools for the demo.
# Add @function_tool decorated functions here and include them in get_tools().


def get_tools() -> list:
    return []
