import asyncio
from uuid import uuid4
import httpx
import pytest
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from arena.app.runtime.context import RuntimeMatch
from arena.app.runtime.docker import DockerRuntime
from portal.app.main import create_app
from portal.app.models import Match, MatchEvent
from portal.app.provisioning import destroy_arena
from portal.app.victory import VictoryEngine
from portal.app import victory
from shared.arena import Observation
from shared.cases import CaseLoader
from arena_support import ObservationProvider
from test_checkpoint_09 import lobby_db, auth
from test_checkpoint_10 import ROOT, FakeArena
from test_checkpoint_12 import game


def test_complete_timeline_restart_pagination_and_privacy(lobby_db, monkeypatch):
    url, engine = lobby_db
    match_id, keys, teams = game(lobby_db)
    ticks = [100.0]
    monkeypatch.setattr(victory, 'monotonic', lambda: ticks[0])
    case = CaseLoader().load(ROOT / 'arena/cases/web-001')
    with Session(engine) as session:
        match = session.get(Match, match_id)
        def observation(health='HEALTHY', exploit='VULNERABLE', login=False):
            return Observation(health={'web': {'status': health, 'reason': 'OK' if health == 'HEALTHY' else 'CHECK_FAILED'}},
                exploit=exploit, surface='AVAILABLE', red_exploit=exploit,
                red_services={'web': 'AVAILABLE'}, duration_seconds=0, blue_login_seen=login)
        provider = ObservationProvider(match, observation())
        controller = VictoryEngine(provider)
        def poll(**values):
            ticks[0] += 1
            provider.observation = observation(**values)
            return asyncio.run(controller.poll(session, match, case))
        poll()
        poll()
        poll(health='UNHEALTHY', exploit='UNREACHABLE')
        poll(login=True)
        poll(exploit='PATCHED', login=True)
        poll(exploit='ERROR', login=True)
        poll(exploit='VULNERABLE', login=True)
        poll(exploit='PATCHED', login=True)
        for _ in range(61):
            result = poll(exploit='PATCHED', login=True)
        assert result['status'] == 'BLUE_KEY_ISSUED'
    with TestClient(create_app(url)) as client:
        headers = auth(teams['BLUE'][0]['token'])
        endpoint = f'/matches/{match_id}/submit'
        assert client.post(endpoint, headers=headers, json={'key': 'wrong'}).json()['result'] == 'INVALID'
        assert client.post(endpoint, headers=headers, json={'key': keys['BLUE']}).json()['result'] == 'BLUE_WIN'
    with Session(engine) as session:
        match = session.get(Match, match_id)
        asyncio.run(destroy_arena(session, match, FakeArena()))
        asyncio.run(destroy_arena(session, match, FakeArena()))
    with TestClient(create_app(url)) as client:
        endpoint = f'/matches/{match_id}/events'
        assert client.get(endpoint).status_code == 401
        outsider = client.post('/lobby/session', json={'nickname': 'outsider'}).json()
        assert client.get(endpoint, headers=auth(outsider['token'])).status_code == 404
        assert client.get(endpoint + '?limit=501', headers=headers).status_code == 422
        events, cursor = [], 0
        while True:
            response = client.get(endpoint, headers=headers, params={'after': cursor, 'limit': 3})
            assert response.headers['cache-control'] == 'no-store'
            page = response.json()
            if not page['events']:
                assert page['cursor'] == cursor
                break
            assert page['cursor'] > cursor
            events.extend(page['events'])
            cursor = page['cursor']
        kinds = [e['type'] for e in events]
        required = ['MATCH_CREATED', 'TARGET_STARTED', 'MATCH_STARTED', 'TARGET_HEALTHY',
            'SERVICE_UP', 'BLUE_FIRST_LOGIN', 'SERVICE_DOWN', 'SERVICE_RESTORED',
            'EXPLOIT_AVAILABLE', 'EXPLOIT_BLOCKED', 'EXPLOIT_RESTORED',
            'BLUE_SECURING_STARTED', 'BLUE_SECURING_CANCELLED', 'BLUE_KEY_ISSUED',
            'KEY_SUBMITTED_INVALID', 'KEY_SUBMITTED_BLUE', 'MATCH_FINISHED', 'ARENA_DESTROYED']
        assert set(required) <= set(kinds)
        for once in ('TARGET_HEALTHY', 'BLUE_FIRST_LOGIN', 'EXPLOIT_AVAILABLE', 'ARENA_DESTROYED'):
            assert kinds.count(once) == 1
        assert kinds.count('EXPLOIT_BLOCKED') == 2
        assert kinds.index('MATCH_FINISHED') < kinds.index('ARENA_DESTROYED')
        assert 'ARENA_OBSERVATION' not in kinds
        assert all(key not in str(events) for key in keys.values())
        stamps = [datetime.fromisoformat(e['timestamp']) for e in events]
        assert stamps == sorted(stamps)
        assert max(stamps) <= datetime.now(timezone.utc)
        assert client.get(endpoint, headers=auth(teams['RED'][0]['token']), params={'limit': 500}).json()['events'] == events


def test_ssh_login_detection_is_cached_and_only_accepts_success(monkeypatch):
    runtime = DockerRuntime(ROOT / 'arena/cases')
    match = RuntimeMatch(1)
    monkeypatch.setattr(runtime, '_resources', lambda *a: ['target-id'])
    lines = ['Failed password for blue from 192.0.2.1', 'Accepted password for blue from 192.0.2.1 port 123 ssh2']
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        assert args == ('logs', '--tail', '200', 'target-id') and kwargs['include_stderr']
        return lines.pop(0)
    monkeypatch.setattr(runtime, '_docker', docker)
    assert not runtime.blue_login_seen(match)
    assert runtime.blue_login_seen(match)
    assert runtime.blue_login_seen(match)
    assert len(calls) == 2


@pytest.mark.parametrize('remote', [False, True])
def test_login_observation_crosses_both_providers(remote):
    from arena.app.main import create_app as create_agent
    from arena.app.provider import LocalArenaProvider
    from arena.app.service import ArenaService
    from portal.app.arena_provider import RemoteArenaProvider
    from shared.arena import CreateMatch
    from test_checkpoint_08a import FakeExecutor, TOKEN, wait_ready, wait_events
    class Executor(FakeExecutor):
        def observe(self, *args):
            return super().observe(*args).model_copy(update={'blue_login_seen': True})
    async def scenario():
        service = ArenaService(Executor(), interval=0.01)
        app = create_agent(service, TOKEN)
        async with app.router.lifespan_context(app):
            provider = RemoteArenaProvider('https://arena.test', TOKEN, transport=httpx.ASGITransport(app=app)) if remote else LocalArenaProvider(service)
            try:
                await provider.create_match(CreateMatch(match_id=1, run_id=uuid4(), case_id='web-001', red_key='secret'))
                await wait_ready(provider, 1)
                page = await wait_events(provider, 1)
                assert page.events[0].observation.blue_login_seen
                assert 'secret' not in page.model_dump_json()
            finally:
                if remote:
                    await provider.close()
    asyncio.run(scenario())
