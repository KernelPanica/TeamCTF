import asyncio
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from portal.app.main import create_app
from portal.app.matchmaking import claim_match
from portal.app.models import Match, MatchState
from portal.app.provisioning import provision_arena
from portal.app.states import transition
from shared.cases import CaseCatalog
from test_checkpoint_09 import lobby_db, auth
from test_checkpoint_10 import FakeArena, ROOT, enter


def prepare_game(url, engine):
    with TestClient(create_app(url)) as client:
        players = enter(client)
    catalog = CaseCatalog(ROOT / 'arena/cases')
    match_id = claim_match(engine, catalog)
    provider = FakeArena()
    provider.ready.set()
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(engine) as session:
        match = session.get(Match, match_id)
        asyncio.run(provision_arena(session, match, catalog.get(match.case_id), provider))
        assert before <= match.started_at <= datetime.now(timezone.utc).replace(tzinfo=None)
        # The browser must show server elapsed time, even with a skewed client clock.
        match.started_at -= timedelta(seconds=3661)
        session.commit()
        started = match.started_at
    return players, match_id, started


def test_game_clock_and_access_boundaries_survive_restart(lobby_db):
    url, engine = lobby_db
    players, match_id, started = prepare_game(url, engine)
    for _ in range(2):
        with TestClient(create_app(url)) as client:
            outsider = client.post('/lobby/session', json={'nickname': f'outsider-{_}'}).json()
            endpoint = f'/matches/{match_id}/access'
            assert client.get(endpoint).status_code == 401
            assert client.get(endpoint, headers=auth(outsider['token'])).status_code == 404
            for player in players:
                headers = auth(player['token'])
                state = client.get('/lobby/session', headers=headers).json()['match']
                assert state['started_at'] == started.isoformat() + 'Z'
                assert 3661 <= state['elapsed_seconds'] < 3700
                assert 'password' not in str(state) and 'key' not in str(state)
                response = client.get(endpoint, headers=headers)
                assert response.headers['cache-control'] == 'no-store'
                access = response.json()
                assert access['services'][0]['port'] == 30003
                assert ('password' in access) == (state['team'] == 'BLUE')
                assert 'blue_key' not in access
    with Session(engine) as session:
        match = session.get(Match, match_id)
        assert match.state == MatchState.RUNNING  # elapsed time never finishes a match
        match.blue_key_issued_at = datetime.now(timezone.utc).replace(tzinfo=None)
        key = match.blue_key
        session.commit()
    with TestClient(create_app(url)) as client:
        for player in players:
            headers = auth(player['token'])
            team = client.get('/lobby/session', headers=headers).json()['match']['team']
            access = client.get(endpoint, headers=headers).json()
            assert access.get('blue_key') == (key if team == 'BLUE' else None)
        with Session(engine) as session:
            transition(session.get(Match, match_id), MatchState.FAILED)
            session.commit()
        assert client.get(endpoint, headers=headers).status_code == 409
        assert client.get('/lobby/session', headers=headers).json()['match']['elapsed_seconds'] is None


@pytest.mark.skipif(os.getenv('RUN_BROWSER') != '1', reason='requires Playwright Chromium')
def test_red_blue_game_screens_refresh_and_revocation(lobby_db, tmp_path):
    from playwright.sync_api import sync_playwright, expect
    url, engine = lobby_db
    players, match_id, _ = prepare_game(url, engine)
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
                pages, errors, accesses = [], [], {}
                for player in players:
                    page = browser.new_context(viewport={'width': 1280, 'height': 900}).new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    responses = []
                    page.on('response', lambda response, received=responses:
                        received.append(response) if response.url.endswith('/access') else None)
                    page.add_init_script("Date.now = () => 0;")
                    page.goto(address)
                    page.evaluate('(token) => sessionStorage.setItem("cyberrange.player-token", token)', player['token'])
                    page.reload()
                    expect(page.locator('#target-host')).to_have_text('192.0.2.2')
                    expect(page.locator('#target-services')).to_contain_text('30003')
                    expect(page.locator('#match-time')).to_have_text(re.compile(r'01:01:\d{2}'))
                    expect(page.locator('#submit-key')).to_be_disabled()
                    pages.append(page)
                    accesses[page] = responses
                assert len({p.locator('#match-id').inner_text() for p in pages}) == 1
                assert sorted(p.locator('#game-team').inner_text() for p in pages) == ['BLUE TEAM'] * 2 + ['RED TEAM'] * 2
                for page in pages:
                    blue = page.locator('#match-team').inner_text() == 'BLUE'
                    if blue:
                        expect(page.locator('#admin-access')).to_be_visible()
                        expect(page.locator('#ssh-command')).to_have_text('ssh -p 30002 blue@192.0.2.2')
                        expect(page.locator('#ssh-password')).to_have_text('temporary-password')
                    else:
                        expect(page.locator('#admin-access')).to_be_hidden()
                        assert page.locator('#ssh-password').text_content() == ''
                        assert all('password' not in r.json() for r in accesses[page])
                    page.reload()
                    expect(page.locator('#target-host')).to_have_text('192.0.2.2')
                with Session(engine) as session:
                    match = session.get(Match, match_id)
                    match.blue_key_issued_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    key = match.blue_key
                    session.commit()
                for page in pages:
                    if page.locator('#match-team').inner_text() == 'BLUE':
                        expect(page.locator('#blue-key')).to_have_text(key, timeout=12000)
                    else:
                        expect(page.locator('#issued-key')).to_be_hidden()
                        assert key not in page.content()
                page = pages[0]
                previous = page.locator('#match-time').inner_text()
                expect(page.locator('#match-time')).not_to_have_text(previous, timeout=2500)
                page.set_viewport_size({'width': 360, 'height': 800})
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path=tmp_path / 'game.png', full_page=True)
                blue_page = next(p for p in pages if p.locator('#match-team').inner_text() == 'BLUE')
                blue_page.route('**/access', lambda route: route.fulfill(
                    status=503, content_type='application/json', body='{"detail":"Unavailable"}'))
                expect(blue_page.locator('#admin-access')).to_be_hidden(timeout=12000)
                assert blue_page.locator('#ssh-password').text_content() == ''
                expect(blue_page.locator('#access-status')).to_contain_text('недоступен')
                blue_page.unroute('**/access')
                expect(blue_page.locator('#ssh-password')).to_have_text('temporary-password', timeout=12000)
                with Session(engine) as session:
                    transition(session.get(Match, match_id), MatchState.FAILED)
                    session.commit()
                for page in pages:
                    expect(page.locator('#game')).to_be_hidden(timeout=12000)
                    assert page.locator('#ssh-password').text_content() == ''
                    assert page.locator('#blue-key').text_content() == ''
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
