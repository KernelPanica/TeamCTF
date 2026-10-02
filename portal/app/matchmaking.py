"""Atomic queue assignment in Portal; provisioning remains a provider operation."""
import asyncio
import logging
import random
import secrets
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from shared.arena import ArenaError
from .cases import CaseCatalog, CaseFormatError
from .models import Case, Match, MatchEvent, MatchPlayer, MatchState, Player, Team
from .provisioning import provision_arena, destroy_arena
from .states import transition
from .victory import VictoryEngine
from .observations import collect_observations


TERMINAL = (MatchState.FINISHED, MatchState.FAILED)
log = logging.getLogger(__name__)


def player_match(session, player_id, *, active_only=False):
    query = select(Match).join(MatchPlayer).where(MatchPlayer.player_id == player_id)
    if active_only:
        query = query.where(Match.state.not_in(TERMINAL))
    return session.scalar(query.order_by(Match.id.desc()).limit(1))


def claim_match(engine, catalog):
    if not catalog.list():
        return None
    with Session(engine) as session:
        # SQLite serializes claiming with queue join/leave/logout writes.
        session.execute(text("BEGIN IMMEDIATE"))
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        active_member = select(MatchPlayer.player_id).join(Match).where(
            MatchPlayer.player_id == Player.id, Match.state.not_in(TERMINAL),
        ).exists()
        players = session.scalars(select(Player).where(
            Player.queued_at.is_not(None), Player.session_expires_at > now,
            Player.token_hash.is_not(None), ~active_member,
        ).order_by(Player.queued_at, Player.id).limit(4)).all()
        if len(players) < 4:
            return None
        seed = secrets.randbits(63)
        case = catalog.random(seed)
        if session.get(Case, case.id) is None:
            session.add(Case(id=case.id, name=case.name))
            session.flush()
        match = Match(state=MatchState.WAITING, case_id=case.id, seed=seed)
        session.add(match)
        session.flush()
        transition(match, MatchState.MATCHMAKING)
        random.Random(seed).shuffle(players)
        for i, player in enumerate(players):
            session.add(MatchPlayer(match_id=match.id, player_id=player.id, team=Team.RED if i < 2 else Team.BLUE))
            player.queued_at = None
        transition(match, MatchState.PROVISIONING)
        session.add(MatchEvent(match_id=match.id, type="MATCH_CREATED",
            event_metadata={"case_id": case.id, "seed": seed}))
        session.commit()
        return match.id


async def run_assigned(engine, provider, match_id, directory):
    with Session(engine) as session:
        match = session.get(Match, match_id)
        if match is None or match.state not in (MatchState.PROVISIONING, MatchState.RUNNING):
            return True
        try:
            case = CaseCatalog(directory).get(match.case_id)
            if match.state == MatchState.PROVISIONING:
                await provision_arena(session, match, case, provider)
            victory = VictoryEngine(provider, directory)
            while True:
                result = await victory.poll(session, match, case)
                if result["status"] == "STOPPED":
                    break
                if result["status"] == "BLUE_KEY_ISSUED":
                    await collect_observations(session, match, provider)
                await asyncio.sleep(2)
            return True
        except ArenaError:
            session.rollback()
            session.refresh(match)
            # Preserve run ID/keys on an uncertain request; retry the same execution.
            return match.state != MatchState.PROVISIONING
        except (OSError, KeyError, CaseFormatError, ValueError):
            session.rollback()
            session.refresh(match)
            if match.state == MatchState.PROVISIONING:
                transition(match, MatchState.FAILED)
                session.add(MatchEvent(match_id=match.id, type="PROVISIONING_FAILED",
                                       event_metadata={"reason": "CASE_UNAVAILABLE"}))
                session.commit()
                if match.arena_run_id:
                    try:
                        await destroy_arena(session, match, provider)
                    except ArenaError:
                        pass  # Reconciler retries the persisted cleanup request.
            return True


async def matchmaker(engine, provider, directory):
    # ponytail: one Portal worker owns provisioning tasks; use durable job leases
    # before supporting multiple Portal worker processes.
    jobs = {}
    offline_match = None
    try:
        while True:
            for match_id, job in list(jobs.items()):
                if job.done():
                    if not job.result():
                        offline_match = match_id
                    del jobs[match_id]
            if offline_match is not None:
                try:
                    await provider.get_match(offline_match)
                except ArenaError as error:
                    if error.status != 404:
                        await asyncio.sleep(5)
                        continue
                offline_match = None
            try:
                catalog = CaseCatalog(directory)
                # Read the catalog again per assignment; invalid cases never enter a match.
                claim_match(engine, catalog)
                with Session(engine) as session:
                    pending = session.scalars(select(Match.id).where(
                        Match.state.in_((MatchState.PROVISIONING, MatchState.RUNNING)),
                        Match.seed.is_not(None),
                    )).all()
                for match_id in pending:
                    if match_id not in jobs:
                        jobs[match_id] = asyncio.create_task(run_assigned(engine, provider, match_id, directory))
            except (OSError, CaseFormatError):
                log.warning("Matchmaking paused: no readable case catalog")
            await asyncio.sleep(1)
    finally:
        for job in jobs.values():
            job.cancel()
        await asyncio.gather(*jobs.values(), return_exceptions=True)
