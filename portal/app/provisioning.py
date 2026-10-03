"""Portal owns keys and game state; execution goes through ArenaProvider."""
import asyncio
import secrets
from uuid import uuid4

from shared.arena import ArenaError, ArenaProvider, CreateMatch
from .models import Case, MatchState, MatchEvent
from sqlalchemy import select
from .states import transition


async def provision_arena(session, match, case, provider: ArenaProvider):
    if match.state != MatchState.PROVISIONING:
        raise ValueError("match must be PROVISIONING")
    if match.case_id is not None and match.case_id != case.id:
        raise ValueError("case does not match the assigned match case")
    if provider is None:
        raise ArenaError("ARENA_NOT_CONFIGURED")
    if not match.arena_run_id:
        if session.get(Case, case.id) is None:
            session.add(Case(id=case.id, name=case.name))
        match.case_id = case.id
        match.arena_run_id = str(uuid4())
        match.red_key = "RED_" + secrets.token_urlsafe(32)
        match.blue_key = "BLUE_" + secrets.token_urlsafe(32)
        match.securing_started_at = match.blue_key_issued_at = None
        match.arena_cleanup_pending = True
        session.commit()
    request = CreateMatch(match_id=match.id, run_id=match.arena_run_id,
                          case_id=case.id, red_key=match.red_key)
    # A known execution must survive a Portal restart unchanged. Never POST a
    # replacement to a restarted Agent that has forgotten this run.
    if match.arena_instance_id:
        try:
            status = await provider.get_match(match.id)
        except ArenaError as error:
            if error.status != 404:
                raise
            from .arena_reconcile import mark_lost
            mark_lost(session, match)
            await destroy_arena(session, match, provider)
            raise ArenaError("ARENA_LOST") from None
    else:
        status = await provider.create_match(request)
    if status.match_id != match.id or str(status.run_id) != match.arena_run_id:
        raise ArenaError("WRONG_EXECUTION")
    if match.arena_instance_id and match.arena_instance_id != str(status.instance_id):
        from .arena_reconcile import mark_lost
        mark_lost(session, match)
        await destroy_arena(session, match, provider)
        raise ArenaError("ARENA_LOST")
    match.arena_instance_id = str(status.instance_id)
    session.commit()
    # Transport errors preserve the durable run ID for an idempotent retry.
    deadline = asyncio.get_running_loop().time() + 900
    while True:
        if status.match_id != match.id or str(status.run_id) != match.arena_run_id:
            raise ArenaError("WRONG_EXECUTION")
        if str(status.instance_id) != match.arena_instance_id:
            from .arena_reconcile import mark_lost
            mark_lost(session, match)
            await destroy_arena(session, match, provider)
            raise ArenaError("ARENA_LOST")
        if status.state != "PROVISIONING":
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise ArenaError("PROVISIONING_TIMEOUT")
        await asyncio.sleep(0.1)
        status = await provider.get_match(match.id)
    if status.state != "READY" or status.blue_password is None:
        transition(match, MatchState.FAILED)
        await destroy_arena(session, match, provider)
        raise ArenaError("PROVISIONING_FAILED")
    ssh = next((e for e in status.endpoints if e.name == "ssh"), None)
    if ssh is None:
        raise ArenaError("MISSING_SSH_ENDPOINT")
    session.refresh(match)
    if match.state != MatchState.PROVISIONING or match.red_key is None:
        await provider.destroy_match(match.id)
        raise ArenaError("PROVISIONING_REVOKED")
    match.target_host = ssh.host
    match.blue_password = status.blue_password.get_secret_value()
    match.arena_endpoints = [e.model_dump() for e in status.endpoints]
    match.arena_instance_id = str(status.instance_id)
    transition(match, MatchState.RUNNING)
    session.add_all([MatchEvent(match_id=match.id, type=kind, timestamp=match.started_at)
                     for kind in ("TARGET_STARTED", "MATCH_STARTED")])
    session.commit()


async def destroy_arena(session, match, provider: ArenaProvider):
    match.target_host = match.blue_password = None
    match.red_key = match.blue_key = None
    match.securing_started_at = match.blue_key_issued_at = None
    match.arena_endpoints = None
    match.arena_cleanup_pending = True
    session.commit()
    if provider is None:
        raise ArenaError("ARENA_NOT_CONFIGURED")
    await provider.destroy_match(match.id)
    match.arena_cleanup_pending = False
    if session.scalar(select(MatchEvent.id).where(
            MatchEvent.match_id == match.id, MatchEvent.type == "ARENA_DESTROYED")) is None:
        session.add(MatchEvent(match_id=match.id, type="ARENA_DESTROYED"))
    session.commit()
