"""Portal persists health transitions; checker execution belongs to Arena."""
import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from .models import MatchEvent, MatchState


HEALTH_EVENTS = ("SERVICE_UP", "SERVICE_DOWN", "SERVICE_RESTORED")


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def record_health(session, match, services, now=None):
    with session.no_autoflush:
        session.refresh(match)
    if match.state != MatchState.RUNNING or not match.target_host:
        session.rollback()
        raise ValueError("arena stopped during health check")
    results = {}
    for name, result in services.items():
        timestamp = now or _utcnow()
        healthy = result["status"] == "HEALTHY"
        previous = session.scalar(select(MatchEvent).where(
            MatchEvent.match_id == match.id, MatchEvent.type.in_(HEALTH_EVENTS),
            MatchEvent.event_metadata["service"].as_string() == name,
        ).order_by(MatchEvent.id.desc()).limit(1))
        downtime = previous.event_metadata["downtime_seconds"] if previous else 0.0
        was_down = previous is not None and previous.type == "SERVICE_DOWN"
        if was_down:
            downtime += max(0.0, (timestamp - previous.timestamp).total_seconds())
        if previous is None or healthy == was_down:
            kind = ("SERVICE_RESTORED" if was_down else "SERVICE_UP") if healthy else "SERVICE_DOWN"
            session.add(MatchEvent(match_id=match.id, type=kind, timestamp=timestamp,
                event_metadata={"service": name, **result, "downtime_seconds": downtime}))
        results[name] = {**result, "downtime_seconds": downtime}
    return {"status": "HEALTHY" if all(r["status"] == "HEALTHY" for r in results.values()) else "UNHEALTHY",
            "services": results}


async def watch_health(engine, match_id, provider, interval=2):
    from sqlalchemy.orm import Session
    from .models import Match
    from .observations import collect_observations
    if interval <= 0:
        raise ValueError("interval must be positive")
    while True:
        with Session(engine) as session:
            match = session.get(Match, match_id)
            if match is None:
                raise ValueError("match not found")
            if match.state != MatchState.RUNNING or not match.target_host:
                return
            result = await collect_observations(session, match, provider)
            if result:
                yield result["health"]
        await asyncio.sleep(interval)
