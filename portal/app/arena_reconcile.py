"""Reconcile lost arenas and retry requested cleanup without consuming events."""
import asyncio
from sqlalchemy import select, or_, update
from sqlalchemy.orm import Session

from shared.arena import ArenaError
from .models import Match, MatchState
from .provisioning import destroy_arena


def mark_lost(session, match):
    # A submission may have finished the match while Arena status was in flight.
    session.execute(update(Match).where(Match.id == match.id,
        Match.state.in_((MatchState.RUNNING, MatchState.PROVISIONING))).values(
        state=MatchState.FAILED, target_host=None, blue_password=None, red_key=None,
        blue_key=None, arena_endpoints=None, securing_started_at=None,
        blue_key_issued_at=None, arena_cleanup_pending=True))
    session.commit()
    session.refresh(match)


async def reconcile_once(engine, provider):
    with Session(engine) as session:
        matches = session.scalars(select(Match).where(Match.arena_run_id.is_not(None), or_(
            Match.state.in_((MatchState.RUNNING, MatchState.PROVISIONING)), Match.arena_cleanup_pending.is_(True),
        ))).all()
        for match in matches:
            try:
                if match.arena_cleanup_pending and match.target_host is None and match.state != MatchState.PROVISIONING:
                    await destroy_arena(session, match, provider)
                    continue
                if match.state in (MatchState.RUNNING, MatchState.PROVISIONING):
                    try:
                        status = await provider.get_match(match.id)
                    except ArenaError as error:
                        if error.status != 404:
                            continue
                        if match.state == MatchState.PROVISIONING and match.arena_instance_id is None:
                            continue
                        mark_lost(session, match)
                    else:
                        if (str(status.run_id) != match.arena_run_id or
                            (match.arena_instance_id is not None and str(status.instance_id) != match.arena_instance_id) or
                            status.state in ("FAILED", "DELETED", "DELETING")):
                            mark_lost(session, match)
                if match.arena_cleanup_pending and (
                    match.state in (MatchState.FAILED, MatchState.FINISHED, MatchState.FINISHING)
                    or (match.state == MatchState.RUNNING and match.target_host is None)
                ):
                    await destroy_arena(session, match, provider)
            except ArenaError:
                session.rollback()


async def reconcile_forever(engine, provider):
    while True:
        await reconcile_once(engine, provider)
        await asyncio.sleep(5)
