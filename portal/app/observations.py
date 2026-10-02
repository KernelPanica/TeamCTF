from datetime import datetime, timezone
from sqlalchemy import update

from shared.arena import ArenaError
from .health import record_health
from .models import Match, MatchEvent, MatchState
from .arena_reconcile import mark_lost


async def collect_observations(session, match, provider):
    if provider is None:
        raise ArenaError("ARENA_NOT_CONFIGURED")
    try:
        status = await provider.get_match(match.id)
    except ArenaError as error:
        if error.status == 404:
            mark_lost(session, match)
        raise
    if (str(status.run_id) != match.arena_run_id or str(status.instance_id) != match.arena_instance_id
            or status.state != "READY"):
        mark_lost(session, match)
        raise ArenaError("ARENA_LOST")
    page = await provider.get_events(match.id, match.arena_cursor)
    if str(page.run_id) != match.arena_run_id or str(page.instance_id) != match.arena_instance_id:
        raise ArenaError("WRONG_EXECUTION")
    session.refresh(match)
    if match.state != MatchState.RUNNING or not match.target_host:
        raise ValueError("arena stopped during check")
    result = None
    cursor = match.arena_cursor
    for event in page.events:
        if event.sequence != cursor + 1:
            raise ArenaError("EVENT_GAP")
        observation = event.observation
        now = datetime.now(timezone.utc)
        age = (now - event.timestamp).total_seconds()
        health = record_health(session, match,
            {name: value.model_dump() for name, value in observation.health.items()},
            min(now, event.timestamp).astimezone(timezone.utc).replace(tzinfo=None))
        session.add(MatchEvent(match_id=match.id, type="ARENA_OBSERVATION",
            event_metadata={"run_id": match.arena_run_id, "sequence": event.sequence,
                            "observation": observation.model_dump(mode="json")}))
        eligible = (bool(observation.health) and health["status"] == "HEALTHY"
            and observation.surface == "AVAILABLE" and observation.red_exploit == "PATCHED"
            and 0 <= age <= 30 and observation.duration_seconds <= 30)
        result = {"health": health, "exploit": observation.exploit,
            "red_zone": {"surface": observation.surface, "exploit": observation.red_exploit,
                         "services": observation.red_services},
            "blue_eligible": eligible and (result is None or result["blue_eligible"])}
        cursor = event.sequence
    if page.cursor != cursor:
        raise ArenaError("INVALID_CURSOR")
    updated = session.execute(update(Match).where(
        Match.id == match.id, Match.arena_cursor == match.arena_cursor,
        Match.arena_run_id == str(page.run_id), Match.state == MatchState.RUNNING,
    ).values(arena_cursor=cursor).execution_options(synchronize_session=False))
    if updated.rowcount != 1:
        session.rollback()
        raise ArenaError("CONCURRENT_POLL")
    session.commit()
    return result
