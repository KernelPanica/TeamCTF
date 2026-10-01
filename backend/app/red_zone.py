"""Portal policy for observations received from the RED zone."""
from .observations import collect_observations


async def check_red_defense(session, match, provider):
    result = await collect_observations(session, match, provider)
    return result or {"pending": True, "blue_eligible": False}
