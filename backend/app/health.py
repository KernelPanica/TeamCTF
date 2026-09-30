"""Functional checks executed by the controller, outside disposable targets."""
import ipaddress
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .cases import CaseLoader, CaseSpec
from .models import Match, MatchEvent, MatchState


HEALTH_EVENTS = ("SERVICE_UP", "SERVICE_DOWN", "SERVICE_RESTORED")


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class HealthChecker:
    def __init__(self, cases_directory: Path = Path("cases"), timeout: float = 5):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.cases_directory = Path(cases_directory).resolve()
        self.timeout = timeout

    def poll(self, session: Session, match: Match, case: CaseSpec) -> dict:
        if match.state != MatchState.RUNNING or not match.target_host:
            raise ValueError("health checks require a running arena")
        if match.case_id != case.id:
            raise ValueError("checker must belong to the match case")
        host = ipaddress.ip_address(match.target_host)
        directory = self.cases_directory / case.id
        if directory.is_symlink() or not directory.resolve().is_relative_to(self.cases_directory):
            raise ValueError("case directory escapes the catalog")
        if CaseLoader().load(directory) != case:
            raise ValueError("case changed; reload the catalog")
        checker = directory / case.checks.health
        address = f"[{host}]" if host.version == 6 else str(host)
        results = {}
        for service in case.required_services:
            # Preserve the original URL argument; extra arguments identify the service.
            url = f"http://{address}:{service.port}"
            try:
                process = subprocess.run(
                    [sys.executable, str(checker), url, service.name, service.protocol],
                    cwd=directory, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=self.timeout,
                )
                healthy = process.returncode == 0
                reason = "OK" if healthy else ("CHECK_FAILED" if process.returncode == 1 else "CHECK_ERROR")
            except subprocess.TimeoutExpired:
                healthy, reason = False, "TIMEOUT"
            except OSError:
                healthy, reason = False, "CHECK_ERROR"
            now = _utcnow()
            previous = session.scalar(
                select(MatchEvent).where(
                    MatchEvent.match_id == match.id,
                    MatchEvent.type.in_(HEALTH_EVENTS),
                    MatchEvent.event_metadata["service"].as_string() == service.name,
                ).order_by(MatchEvent.id.desc()).limit(1)
            )
            downtime = previous.event_metadata["downtime_seconds"] if previous else 0.0
            was_down = previous is not None and previous.type == "SERVICE_DOWN"
            if was_down:
                downtime += max(0.0, (now - previous.timestamp).total_seconds())
            status = "HEALTHY" if healthy else "UNHEALTHY"
            if previous is None or healthy == was_down:
                event_type = ("SERVICE_RESTORED" if was_down else "SERVICE_UP") if healthy else "SERVICE_DOWN"
                session.add(MatchEvent(
                    match_id=match.id, type=event_type, timestamp=now,
                    event_metadata={"service": service.name, "status": status,
                                    "downtime_seconds": downtime, "reason": reason},
                ))
            results[service.name] = {"status": status, "downtime_seconds": downtime, "reason": reason}
        # Do not append observations after another controller has finished/revoked the arena.
        with session.no_autoflush:
            session.refresh(match)
        if match.state != MatchState.RUNNING or not match.target_host:
            session.rollback()
            raise ValueError("arena stopped during health check")
        session.commit()
        return {"status": "HEALTHY" if all(result["status"] == "HEALTHY" for result in results.values()) else "UNHEALTHY",
                "services": results}


def watch_health(engine, match_id: int, cases_directory: Path = Path("cases"), interval: float = 2):
    """One controller per match; stop polling when the arena leaves RUNNING."""
    # ponytail: one watcher per match; serialize polls if multiple controllers are introduced.
    if interval <= 0:
        raise ValueError("interval must be positive")
    checker = HealthChecker(cases_directory)
    while True:
        with Session(engine) as session:
            match = session.get(Match, match_id)
            if match is None:
                raise ValueError("match not found")
            if match.state != MatchState.RUNNING or not match.target_host:
                return
            if not match.case_id:
                raise ValueError("match has no case")
            case = CaseLoader().load(Path(cases_directory) / match.case_id)
            yield checker.poll(session, match, case)
        time.sleep(interval)
