import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.request import urlopen

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.app.arena_reconcile import mark_lost, reconcile_once
from portal.app.main import create_app
from portal.app.models import Match, MatchEvent, MatchPlayer, MatchState
from shared.arena import ArenaError
from test_checkpoint_09 import lobby_db, auth
from test_checkpoint_10 import FakeArena, ROOT, enter, wait_status
from test_checkpoint_11 import prepare_game


def game(lobby_db):
    url, engine = lobby_db
    players, match_id, _ = prepare_game(url, engine)
    with Session(engine) as session:
        match = session.get(Match, match_id)
        keys = {'RED': match.red_key, 'BLUE': match.blue_key}
        members = {member.player_id: member.team for member in session.scalars(select(MatchPlayer))}
    teams = {team: [p for p in players if members[p['player']['id']] == team] for team in keys}
    return match_id, keys, teams


@pytest.mark.parametrize('winner', ['RED', 'BLUE'])
def test_invalid_team_unissued_keys_and_persistent_first_winner(lobby_db, winner):
    url, engine = lobby_db
    match_id, keys, teams = game(lobby_db)
    endpoint = f'/matches/{match_id}/submit'
    with TestClient(create_app(url)) as client:
        outsider = client.post('/lobby/session', json={'nickname': 'outsider'}).json()
        assert client.post(endpoint, json={'key': keys['RED']}).status_code == 401
        assert client.post(endpoint, headers=auth(outsider['token']), json={'key': keys['RED']}).status_code == 404
        for team in teams:
            headers = auth(teams[team][0]['token'])
            for wrong in ['', 'wrong', 'неверный', keys['BLUE' if team == 'RED' else 'RED']]:
                response = client.post(endpoint, headers=headers, json={'key': wrong})
                assert response.json() == {'result': 'INVALID'}
                assert response.headers['cache-control'] == 'no-store'
        assert client.post(endpoint, headers=auth(teams['BLUE'][0]['token']), json={'key': keys['BLUE']}).json()['result'] == 'INVALID'
        with Session(engine) as session:
            match = session.get(Match, match_id)
            assert match.state == MatchState.RUNNING and match.winner is None
            match.blue_key_issued_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.commit()
        headers = auth(teams[winner][0]['token'])
        assert client.post(endpoint, headers=headers, json={'key': 'x' * 4097}).status_code == 422
        assert client.post(endpoint, headers=headers, json={'key': keys[winner], 'winner': winner}).status_code == 422
        assert client.post(endpoint, headers=headers, json={'key': keys[winner]}).json() == {'result': f'{winner}_WIN', 'winner': winner}
    # Restart, teammate replay and opposing key cannot change the result.
    with TestClient(create_app(url)) as client:
        for team, players in teams.items():
            for player in players:
                headers = auth(player['token'])
                assert client.post(endpoint, headers=headers, json={'key': keys[team]}).json() == {'result': 'MATCH_ALREADY_FINISHED', 'winner': winner}
                state = client.get('/lobby/session', headers=headers).json()['match']
                assert state['winner'] == winner and state['state'] == 'FINISHED'
                assert state['elapsed_seconds'] >= 3661
                assert client.get(f'/matches/{match_id}/access', headers=headers).status_code == 409
    with Session(engine) as session:
        match = session.get(Match, match_id)
        finished = match.finished_at
        assert finished and match.winner == winner and match.arena_cleanup_pending
        assert match.red_key is match.blue_key is match.blue_password is match.target_host is None
        mark_lost(session, match)
        assert match.state == MatchState.FINISHED and match.finished_at == finished
        events = session.scalars(select(MatchEvent).where(MatchEvent.match_id == match_id)).all()
        assert sum(e.type == 'MATCH_FINISHED' for e in events) == 1
        assert sum(e.type == f'KEY_SUBMITTED_{winner}' for e in events) == 1
        assert all(key not in str([e.event_metadata for e in events]) for key in keys.values())


@pytest.mark.parametrize('same_team', [False, True])
def test_simultaneous_submissions_have_exactly_one_winner(lobby_db, same_team):
    url, engine = lobby_db
    match_id, keys, teams = game(lobby_db)
    with Session(engine) as session:
        session.get(Match, match_id).blue_key_issued_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.commit()
    barrier = threading.Barrier(2)
    with TestClient(create_app(url)) as client:
        def submit(team):
            barrier.wait(timeout=5)
            return client.post(f'/matches/{match_id}/submit', headers=auth(teams[team][0]['token']), json={'key': keys[team]}).json()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, ['RED', 'RED' if same_team else 'BLUE']))
    assert sum(r['result'] == 'MATCH_ALREADY_FINISHED' for r in results) == 1
    assert sum(r['result'] in ('RED_WIN', 'BLUE_WIN') for r in results) == 1
    assert results[0]['winner'] == results[1]['winner']
    with Session(engine) as session:
        assert session.get(Match, match_id).winner == results[0]['winner']
        assert len(session.scalars(select(MatchEvent).where(MatchEvent.type == 'MATCH_FINISHED')).all()) == 1


def test_cleanup_failure_does_not_undo_result_and_nonrunning_rejects(lobby_db):
    url, engine = lobby_db
    match_id, keys, teams = game(lobby_db)
    headers = auth(teams['RED'][0]['token'])
    endpoint = f'/matches/{match_id}/submit'
    with TestClient(create_app(url)) as client:
        for state in (MatchState.PROVISIONING, MatchState.FAILED):
            with Session(engine) as session:
                session.get(Match, match_id).state = state
                session.commit()
            assert client.post(endpoint, headers=headers, json={'key': keys['RED']}).status_code == 409
        with Session(engine) as session:
            session.get(Match, match_id).state = MatchState.RUNNING
            session.commit()
        assert client.post(endpoint, headers=headers, json={'key': keys['RED']}).json()['result'] == 'RED_WIN'
    class OfflineArena(FakeArena):
        async def destroy_match(self, match_id):
            raise ArenaError('UNAVAILABLE')
    asyncio.run(reconcile_once(engine, OfflineArena()))
    with Session(engine) as session:
        match = session.get(Match, match_id)
        assert match.winner == 'RED' and match.arena_cleanup_pending
    asyncio.run(reconcile_once(engine, FakeArena()))
    with Session(engine) as session:
        match = session.get(Match, match_id)
        assert match.winner == 'RED' and not match.arena_cleanup_pending


@pytest.mark.skipif(os.getenv('RUN_DOCKER') != '1', reason='requires real Docker')
def test_winning_submission_removes_real_arena(lobby_db, monkeypatch):
    from arena.app.runtime.docker import DockerRuntime
    url, engine = lobby_db
    monkeypatch.setenv('ARENA_PROVIDER', 'local')
    with TestClient(create_app(url)) as client:
        players = enter(client)
        states = [wait_status(client, player, 'RUNNING', timeout=900) for player in players]
        match_id = states[0]['match']['id']
        red = next(p for p, s in zip(players, states) if s['match']['team'] == 'RED')
        with Session(engine) as session:
            key = session.get(Match, match_id).red_key
        response = client.post(f'/matches/{match_id}/submit', headers=auth(red['token']), json={'key': key})
        assert response.json()['result'] == 'RED_WIN'
        deadline = time.monotonic() + 30
        while True:
            with Session(engine) as session:
                match = session.get(Match, match_id)
                assert match.state == MatchState.FINISHED and match.winner == 'RED'
                if not match.arena_cleanup_pending:
                    break
            assert time.monotonic() < deadline, 'runtime cleanup did not finish'
            time.sleep(0.1)
        runtime = DockerRuntime(ROOT / 'arena/cases')
        for kind in ('container', 'network', 'volume'):
            assert runtime._resources(kind, str(match_id)) == []


@pytest.mark.skipif(os.getenv('RUN_BROWSER') != '1', reason='requires Playwright Chromium')
def test_browser_submission_updates_all_four_players(lobby_db):
    from playwright.sync_api import sync_playwright, expect
    url, engine = lobby_db
    match_id, keys, teams = game(lobby_db)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'portal.app.main:app',
            '--fd', str(listener.fileno())], pass_fds=(listener.fileno(),), cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    address = f'http://127.0.0.1:{port}'
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                with urlopen(address + '/health', timeout=1):
                    break
            except OSError:
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.1)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                pages, errors = [], []
                for player in teams['RED'] + teams['BLUE']:
                    page = browser.new_context().new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(address)
                    page.evaluate('(token) => sessionStorage.setItem("cyberrange.player-token", token)', player['token'])
                    page.reload()
                    expect(page.locator('#target-host')).to_have_text('192.0.2.2')
                    pages.append(page)
                red = pages[0]
                red.get_by_label('KEY', exact=True).fill('wrong')
                red.get_by_role('button', name='SUBMIT', exact=True).click()
                expect(red.locator('#submission-result')).to_contain_text('Неверный ключ')
                expect(red.locator('#state-label')).to_have_text('RUNNING')
                red.get_by_label('KEY', exact=True).fill(keys['RED'])
                red.get_by_role('button', name='SUBMIT', exact=True).click()
                for page in pages:
                    expect(page.locator('#match-result')).to_have_text('WINNER: RED', timeout=12000)
                    expect(page.locator('#game')).to_be_hidden()
                    expect(page.locator('#play')).to_be_visible()
                    assert page.locator('#ssh-password').text_content() == ''
                    assert keys['RED'] not in page.url
                    page.reload()
                    expect(page.locator('#match-result')).to_have_text('WINNER: RED')
                assert errors == []
            finally:
                browser.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
