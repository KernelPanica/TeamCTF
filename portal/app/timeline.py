"""Participant-only, bounded access to the persistent game timeline."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session

from .access import authenticated_player, bearer
from .models import Match, MatchEvent, MatchPlayer
from .analyzer import analyze_match

router = APIRouter()


@router.get('/matches/{match_id}/report')
def get_report(match_id: int, request: Request, response: Response,
               credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    response.headers['Cache-Control'] = 'no-store'
    with Session(request.app.state.engine) as session:
        player = authenticated_player(session, credentials)
        if session.get(MatchPlayer, (match_id, player.id)) is None:
            raise HTTPException(404, 'Match not found')
        match = session.get(Match, match_id)
        events = session.scalars(select(MatchEvent).where(
            MatchEvent.match_id == match_id, MatchEvent.type != 'ARENA_OBSERVATION')).all()
        try:
            return analyze_match(match, events)
        except ValueError:
            raise HTTPException(409, 'Report requires a finished match with recorded start/end times')


@router.get('/matches/{match_id}/events')
def get_events(match_id: int, request: Request, response: Response,
               after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500),
               credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    response.headers['Cache-Control'] = 'no-store'
    with Session(request.app.state.engine) as session:
        player = authenticated_player(session, credentials)
        if session.get(MatchPlayer, (match_id, player.id)) is None:
            raise HTTPException(404, 'Match not found')
        events = session.scalars(select(MatchEvent).where(
            MatchEvent.match_id == match_id, MatchEvent.id > after,
            MatchEvent.type != 'ARENA_OBSERVATION',
        ).order_by(MatchEvent.id).limit(limit)).all()
        return {'events': [{'id': e.id, 'type': e.type, 'timestamp': e.timestamp.isoformat() + 'Z',
                            'metadata': e.event_metadata} for e in events],
                'cursor': events[-1].id if events else after}
