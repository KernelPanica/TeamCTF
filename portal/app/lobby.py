"""Anonymous sessions, persistent queue and role-scoped matchmaking status."""
import unicodedata
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .access import authenticated_player, bearer, issue_player_token
from .models import Player, MatchPlayer
from .matchmaking import player_match, TERMINAL


router = APIRouter(prefix="/lobby")
SESSION_TTL = timedelta(minutes=15)


class JoinLobby(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nickname: str

    @field_validator("nickname")
    @classmethod
    def valid_nickname(cls, value):
        value = unicodedata.normalize("NFKC", value).strip()
        if not 2 <= len(value) <= 24 or not all(c.isalnum() or c in "_-" for c in value):
            raise ValueError("Nickname: 2–24 буквы, цифры, _ или -")
        return value


def expire_sessions(session: Session, now: datetime):
    session.execute(update(Player).where(Player.session_expires_at <= now).values(
        active_nickname=None, token_hash=None, queued_at=None, session_expires_at=None,
    ))


def snapshot(session: Session, player: Player, now: datetime) -> dict:
    count = session.scalar(select(func.count()).select_from(Player).where(
        Player.queued_at.is_not(None), Player.session_expires_at > now,
    ))
    result = {"player": {"id": player.id, "nickname": player.nickname},
            "status": "SEARCHING" if player.queued_at is not None else "IDLE",
            "queued_players": count}
    match = player_match(session, player.id)
    if match is not None and player.queued_at is None:
        member = session.get(MatchPlayer, (match.id, player.id))
        allies = session.scalars(select(Player).join(MatchPlayer).where(
            MatchPlayer.match_id == match.id, MatchPlayer.team == member.team,
            Player.id != player.id,
        ).order_by(Player.id)).all()
        result["match"] = {"id": match.id, "case_id": match.case_id, "state": match.state,
                           "team": member.team, "allies": [{"id": p.id, "nickname": p.nickname} for p in allies]}
        if match.state not in TERMINAL:
            result["status"] = "MATCHED"
    return result


@router.post("/session", status_code=201)
def create_session(body: JoinLobby, request: Request, response: Response):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    response.headers["Cache-Control"] = "no-store"
    with Session(request.app.state.engine) as session:
        expire_sessions(session, now)
        player = Player(nickname=body.nickname, active_nickname=body.nickname.casefold(),
                        session_expires_at=now + SESSION_TTL)
        token = issue_player_token(player)
        session.add(player)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, "Nickname уже занят активным игроком")
        return snapshot(session, player, now) | {"token": token}


def lobby_action(request, response, credentials, action):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    response.headers["Cache-Control"] = "no-store"
    with Session(request.app.state.engine) as session:
        player = authenticated_player(session, credentials)
        expire_sessions(session, now)
        match = player_match(session, player.id, active_only=True)
        if match is not None and action in ("logout", "join", "leave"):
            raise HTTPException(409, "Матч уже создан. Дождитесь его завершения")
        if action == "logout":
            player.active_nickname = player.token_hash = player.queued_at = player.session_expires_at = None
            session.commit()
            return {"status": "SIGNED_OUT"}
        player.session_expires_at = now + SESSION_TTL
        if action == "join" and player.queued_at is None:
            player.queued_at = now
        elif action == "leave":
            player.queued_at = None
        session.commit()
        return snapshot(session, player, now)


@router.get("/session")
def get_session(request: Request, response: Response, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    return lobby_action(request, response, credentials, "read")


@router.delete("/session")
def delete_session(request: Request, response: Response, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    return lobby_action(request, response, credentials, "logout")


@router.post("/queue")
def join_queue(request: Request, response: Response, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    return lobby_action(request, response, credentials, "join")


@router.delete("/queue")
def leave_queue(request: Request, response: Response, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    return lobby_action(request, response, credentials, "leave")
