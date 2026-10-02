"""Accept the first valid team key in one SQLite transaction."""
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from .access import authenticated_player, bearer
from .models import Match, MatchEvent, MatchPlayer, MatchState, Team
from .states import transition

router = APIRouter()


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: SecretStr = Field(max_length=4096)


@router.post("/matches/{match_id}/submit")
def submit_key(match_id: int, body: Submission, request: Request, response: Response,
               credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    response.headers["Cache-Control"] = "no-store"
    with Session(request.app.state.engine) as session:
        # Lock before reading: competing requests see the committed first winner.
        # No Arena/network call is made while this transaction holds the lock.
        session.execute(text("BEGIN IMMEDIATE"))
        player = authenticated_player(session, credentials)
        member = session.get(MatchPlayer, (match_id, player.id))
        if member is None or member.team not in (Team.RED, Team.BLUE):
            raise HTTPException(404, "Match not found")
        match = session.get(Match, match_id)
        if match.state in (MatchState.FINISHING, MatchState.FINISHED):
            return {"result": "MATCH_ALREADY_FINISHED", "winner": match.winner}
        if match.state != MatchState.RUNNING:
            raise HTTPException(409, "Match is not running")
        expected = match.red_key if member.team == Team.RED else (
            match.blue_key if match.blue_key_issued_at is not None else None)
        valid = expected is not None and secrets.compare_digest(
            body.key.get_secret_value().encode(), expected.encode())
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        session.add(MatchEvent(match_id=match.id,
            type=f"KEY_SUBMITTED_{member.team}" if valid else "KEY_SUBMITTED_INVALID",
            timestamp=now, event_metadata={"player_id": player.id, "team": member.team}))
        if not valid:
            session.commit()
            return {"result": "INVALID"}
        transition(match, MatchState.FINISHING)
        match.winner, match.finished_at = member.team, now
        transition(match, MatchState.FINISHED)
        session.add(MatchEvent(match_id=match.id, type="MATCH_FINISHED", timestamp=now,
                               event_metadata={"winner": member.team}))
        # Revoke access atomically; the existing reconciler retries runtime cleanup.
        match.target_host = match.blue_password = match.red_key = match.blue_key = None
        match.arena_endpoints = None
        match.arena_cleanup_pending = True
        session.commit()
        return {"result": f"{member.team}_WIN", "winner": member.team}
