"""Continuous BLUE stabilization; issuing a key does not finish the match."""
import math
import asyncio
import time
from time import monotonic
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .cases import CaseLoader, CaseSpec
from .models import Match, MatchEvent, MatchState
from .red_zone import check_red_defense
from shared.arena import ArenaProvider, ArenaError


MAX_POLL_GAP = 30


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class VictoryEngine:
    def __init__(self, provider: ArenaProvider, cases_directory: Path = Path("arena/cases")):
        self.provider = provider
        self.cases_directory = Path(cases_directory)
        self._match_id = None
        self._started = None
        self._last_success = None

    async def poll(self, session: Session, match: Match, case: CaseSpec) -> dict:
        # ponytail: one controller per match; serialize polls before introducing multiple workers.
        if self._match_id not in (None, match.id):
            raise ValueError("use one VictoryEngine per match")
        self._match_id = match.id
        try:
            session.refresh(match)
        except SQLAlchemyError:
            self._started = self._last_success = None
            raise
        if match.state != MatchState.RUNNING or not match.target_host:
            self._started = self._last_success = None
            return {"status": "STOPPED"}
        if not match.red_key or not match.blue_key:
            raise ValueError("match has no keys; provision a new arena")
        if match.case_id != case.id:
            raise ValueError("case does not match the assigned match case")
        if match.blue_key_issued_at is not None:
            return {"status": "BLUE_KEY_ISSUED"}

        began = monotonic()
        reason = "CHECK_FAILED"
        try:
            result = await check_red_defense(session, match, self.provider)
            if result.get("pending") and (self._last_success is None or monotonic() - self._last_success <= MAX_POLL_GAP):
                if self._started is not None:
                    return {"status": "SECURING", "remaining_seconds": max(0, math.ceil(case.blue.stabilization_seconds - (monotonic() - self._started)))}
                # A restart with persisted securing state must still cancel it.
                if match.securing_started_at is None:
                    return {"status": "UNSECURED"}
            eligible = result["blue_eligible"] is True
        except (ArenaError, OSError, ValueError, SQLAlchemyError):
            session.rollback()
            eligible, reason = False, "CHECK_ERROR"
        try:
            session.refresh(match)
        except SQLAlchemyError:
            self._started = self._last_success = None
            raise
        now = monotonic()
        timestamp = _utcnow()
        if match.state != MatchState.RUNNING or not match.target_host:
            self._started = self._last_success = None
            return {"status": "STOPPED"}
        if now - began > MAX_POLL_GAP:
            eligible, reason = False, "CHECK_GAP"
        restarted = match.securing_started_at is not None and self._started is None
        gap = self._last_success is not None and now - self._last_success > MAX_POLL_GAP
        cancelled = False

        def event(kind, **metadata):
            session.add(MatchEvent(match_id=match.id, type=kind, timestamp=timestamp,
                                   event_metadata=metadata))

        if match.securing_started_at is not None and (not eligible or restarted or gap):
            event("BLUE_SECURING_CANCELLED", reason="CONTROLLER_RESTART" if restarted else "CHECK_GAP" if gap else reason)
            match.securing_started_at = None
            self._started = self._last_success = None
            cancelled = True
        if not eligible:
            self._started = self._last_success = None
            status = {"status": "SECURING_CANCELLED" if cancelled else "UNSECURED"}
        else:
            if match.securing_started_at is None:
                match.securing_started_at = timestamp
                self._started = now
                event("BLUE_SECURING_STARTED", stabilization_seconds=case.blue.stabilization_seconds)
            self._last_success = now
            elapsed = now - self._started
            if elapsed >= case.blue.stabilization_seconds:
                match.blue_key_issued_at = timestamp
                event("BLUE_SECURED_TARGET", stabilization_seconds=case.blue.stabilization_seconds)
                event("BLUE_KEY_ISSUED")
                status = {"status": "BLUE_KEY_ISSUED"}
            else:
                status = {"status": "SECURING", "remaining_seconds": math.ceil(case.blue.stabilization_seconds - elapsed)}
        session.commit()
        return status


async def watch_victory(engine, match_id: int, provider, interval: float = 2, cases_directory: Path = Path("arena/cases")):
    if not 0 < interval < MAX_POLL_GAP:
        raise ValueError(f"interval must be between 0 and {MAX_POLL_GAP} seconds")
    victory = VictoryEngine(provider, cases_directory)
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
            result = await victory.poll(session, match, case)
            yield result
            if result["status"] in ("BLUE_KEY_ISSUED", "STOPPED"):
                return
        await asyncio.sleep(interval)
