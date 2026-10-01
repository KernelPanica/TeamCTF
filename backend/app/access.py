"""Shared player authentication and team-scoped arena access."""
import hashlib
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Match, MatchPlayer, MatchState, Player, Team


router = APIRouter()
bearer = HTTPBearer(auto_error=False)


def issue_player_token(player: Player) -> str:
    """Return once to the player; caller commits the hash with the player."""
    token = secrets.token_urlsafe(32)
    player.token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token


def authenticated_player(session: Session, credentials: HTTPAuthorizationCredentials | None) -> Player:
    if credentials is None:
        raise HTTPException(401, "Player token required", headers={"WWW-Authenticate": "Bearer"})
    token_hash = hashlib.sha256(credentials.credentials.encode()).hexdigest()
    player = session.scalar(select(Player).where(Player.token_hash == token_hash))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if player is None or (player.session_expires_at is not None and player.session_expires_at <= now):
        raise HTTPException(401, "Invalid player token", headers={"WWW-Authenticate": "Bearer"})
    return player


class ArenaAccess(BaseModel):
    host: str
    port: int | None = None
    username: str | None = None
    password: str | None = Field(default=None, repr=False)
    blue_key: str | None = Field(default=None, repr=False)
    services: list[dict] | None = None


@router.get("/matches/{match_id}/access", response_model=ArenaAccess, response_model_exclude_none=True)
def get_access(
    match_id: int, request: Request, response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
):
    with Session(request.app.state.engine) as session:
        player = authenticated_player(session, credentials)
        member = session.get(MatchPlayer, (match_id, player.id))
        if member is None or member.team not in (Team.RED, Team.BLUE):
            raise HTTPException(404, "Match not found")
        match = session.get(Match, match_id)
        if match.state != MatchState.RUNNING or not match.target_host or not match.blue_password:
            raise HTTPException(409, "Arena access is unavailable")
        response.headers["Cache-Control"] = "no-store"
        services = None
        ssh_port = 22
        if match.arena_endpoints:
            services = [e for e in match.arena_endpoints if e["name"] != "ssh"]
            ssh_port = next(e["port"] for e in match.arena_endpoints if e["name"] == "ssh")
        if member.team == Team.BLUE:
            return ArenaAccess(
                host=match.target_host, port=ssh_port, username="blue", password=match.blue_password,
                services=services,
                blue_key=match.blue_key if match.blue_key_issued_at is not None else None,
            )
        return ArenaAccess(host=match.target_host, services=services)
