import asyncio
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from arena.app.service import ArenaService
from portal.app.arena_reconcile import reconcile_once
from portal.app.main import create_app
from portal.app.models import Case, Match, MatchEvent, MatchState
from portal.app.provisioning import provision_arena
from shared.arena import ArenaError, CreateMatch
from shared.cases import CaseLoader
from test_checkpoint_09 import lobby_db, auth
from test_checkpoint_10 import FakeArena, ROOT
from test_checkpoint_12 import game
from test_checkpoint_08a import FakeExecutor


@pytest.mark.parametrize('lost', [False, True, 'replaced'])
def test_portal_resume_never_recreates_known_execution(lobby_db, lost):
    _, engine = lobby_db
    provider = FakeArena()
    provider.ready.set()
    case = CaseLoader().load(ROOT / 'arena/cases/web-001')
    request = CreateMatch(match_id=1, run_id=uuid4(), case_id=case.id, red_key='RED_original')
    asyncio.run(provider.create_match(request))
    with Session(engine) as session:
        session.add(Case(id=case.id, name=case.name))
        session.flush()
        session.add(Match(id=1, state=MatchState.PROVISIONING, case_id=case.id,
            arena_run_id=str(request.run_id), arena_instance_id=str(provider.instance),
            red_key='RED_original', blue_key='BLUE_original', arena_cleanup_pending=True))
        session.commit()
    if lost:
        if lost is True:
            provider.requests.clear()  # Agent startup cleaned the old runtime.
        provider.instance = uuid4()
    with Session(engine) as restarted:
        match = restarted.get(Match, 1)
        if lost:
            with pytest.raises(ArenaError, match='ARENA_LOST'):
                asyncio.run(provision_arena(restarted, match, case, provider))
            assert match.state == MatchState.FAILED and match.red_key is None
            assert not match.arena_cleanup_pending
        else:
            asyncio.run(provision_arena(restarted, match, case, provider))
            assert match.state == MatchState.RUNNING and match.red_key == 'RED_original'
        assert match.arena_run_id == str(request.run_id)
    assert provider.calls == 1  # Only the original create; resume uses GET.


def test_report_and_history_survive_pending_cleanup_and_restart(lobby_db):
    url, engine = lobby_db
    match_id, keys, teams = game(lobby_db)
    headers = auth(teams['RED'][0]['token'])
    with TestClient(create_app(url)) as client:
        assert client.post(f'/matches/{match_id}/submit', headers=headers,
                           json={'key': keys['RED']}).json()['result'] == 'RED_WIN'
        report = client.get(f'/matches/{match_id}/report', headers=headers).json()
    with Session(engine) as session:
        match = session.get(Match, match_id)
        assert match.report == report and match.arena_cleanup_pending
    class Offline(FakeArena):
        async def destroy_match(self, match_id):
            raise ArenaError('OFFLINE')
    assert asyncio.run(reconcile_once(engine, Offline())) == 1
    # Startup performs pending cleanup before starting the matchmaker.
    with TestClient(create_app(url, arena_provider=FakeArena())) as client:
        assert client.get(f'/matches/{match_id}/report', headers=headers).json() == report
        with Session(engine) as session:
            match = session.get(Match, match_id)
            assert not match.arena_cleanup_pending and match.winner == 'RED'
            assert session.scalar(select(MatchEvent.id).where(MatchEvent.type == 'MATCH_FINISHED'))
            assert session.scalar(select(MatchEvent.id).where(MatchEvent.type == 'ARENA_DESTROYED'))
    assert asyncio.run(reconcile_once(engine, FakeArena())) == 0
    with Session(engine) as session:
        assert len(session.scalars(select(MatchEvent).where(MatchEvent.type == 'ARENA_DESTROYED')).all()) == 1


@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_agent_cleans_partial_creation_even_without_portal(cleanup_fails):
    class Broken(FakeExecutor):
        def create(self, request):
            self.created.append(request.match_id)
            raise RuntimeError('partial provisioning')
    async def scenario():
        executor = Broken()
        executor.fail_destroy = cleanup_fails
        service = ArenaService(executor)
        await service.start()
        await service.create_match(CreateMatch(match_id=1, run_id=uuid4(), case_id='web-001', red_key='secret'))
        await service.matches[1].task
        status = await service.get_match(1)
        assert status.state == 'FAILED' and status.blue_password is None
        assert status.error == ('CLEANUP_FAILED' if cleanup_fails else 'EXECUTION_FAILED')
        if not cleanup_fails:
            assert executor.destroyed == [1]
        executor.fail_destroy = False
        await service.destroy_match(1)
        assert (await service.get_match(1)).state == 'DELETED'
        await service.close()
    asyncio.run(scenario())


def test_recovery_commands_use_correct_boundary(monkeypatch, lobby_db, capsys):
    from arena.app import cli as arena_cli
    from portal.app import cli as portal_cli
    owners = []
    class Runtime:
        def __init__(self, *, owner):
            owners.append(owner)
        def cleanup_owned(self):
            pass
    monkeypatch.setattr(arena_cli, 'DockerRuntime', Runtime)
    monkeypatch.setenv('ARENA_OWNER', 'test-owner')
    monkeypatch.setattr('sys.argv', ['arena', 'reconcile'])
    assert arena_cli.main() == 0 and owners == ['test-owner']
    monkeypatch.delenv('ARENA_OWNER')
    with pytest.raises(SystemExit):
        arena_cli.main()
    monkeypatch.setenv('ARENA_PROVIDER', 'local')
    monkeypatch.setattr('sys.argv', ['cyberrange', 'portal', 'arena', 'recovery'])
    assert portal_cli.main() == 1
    assert 'RemoteArenaProvider' in capsys.readouterr().out


def test_legacy_match_can_finish_without_fabricating_report(lobby_db):
    url, engine = lobby_db
    match_id, keys, teams = game(lobby_db)
    with Session(engine) as session:
        session.get(Match, match_id).started_at = None
        session.commit()
    with TestClient(create_app(url)) as client:
        headers = auth(teams['RED'][0]['token'])
        assert client.post(f'/matches/{match_id}/submit', headers=headers,
                           json={'key': keys['RED']}).json()['result'] == 'RED_WIN'
        assert client.get(f'/matches/{match_id}/report', headers=headers).status_code == 409
    assert asyncio.run(reconcile_once(engine, FakeArena())) == 0
    with Session(engine) as session:
        match = session.get(Match, match_id)
        assert match.winner == 'RED' and match.report is None and not match.arena_cleanup_pending


@pytest.mark.skipif(os.getenv('RUN_DOCKER') != '1', reason='requires real Docker')
def test_failed_provisioning_removes_real_partial_resources():
    from arena.app.runtime.docker import DockerRuntime
    from arena.app.runtime.context import RuntimeMatch
    runtime = DockerRuntime(ROOT / 'arena/cases', owner='test-failure-' + uuid4().hex)
    case = CaseLoader().load(ROOT / 'arena/cases/web-001')
    match_id = 2**50 + int(uuid4().hex[:10], 16)
    class Broken(FakeExecutor):
        def cleanup(self):
            runtime.cleanup_owned()
        def create(self, request):
            runtime.prepare(RuntimeMatch(request.match_id, case.id), case)
            raise RuntimeError('crash after prepare')
        def destroy(self, match_id):
            runtime.destroy(RuntimeMatch(match_id))
    async def scenario():
        service = ArenaService(Broken())
        await service.start()
        try:
            await service.create_match(CreateMatch(match_id=match_id, run_id=uuid4(), case_id=case.id, red_key='secret'))
            await service.matches[match_id].task
            assert (await service.get_match(match_id)).state == 'FAILED'
            for kind in ('container', 'network', 'volume'):
                assert runtime._resources(kind, str(match_id)) == []
        finally:
            await service.close()
    asyncio.run(scenario())
