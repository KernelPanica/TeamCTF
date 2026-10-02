import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.app.main import create_app
from portal.app.matchmaking import claim_match
from portal.app.models import Match, MatchPlayer, MatchState, Player, Team
from shared.arena import ArenaError, ArenaStatus, Endpoint, EventPage
from shared.cases import CaseCatalog
from test_checkpoint_09 import lobby_db, auth


ROOT = Path(__file__).resolve().parents[1]


class FakeArena:
    def __init__(self):
        self.instance = uuid4()
        self.requests = {}
        self.ready = threading.Event()
        self.offline = False
        self.fail = False
        self.calls = 0
        self.observations = 0

    async def create_match(self, request):
        self.calls += 1
        if self.offline:
            raise ArenaError("UNAVAILABLE")
        if request.match_id in self.requests:
            assert self.requests[request.match_id] == request
        self.requests[request.match_id] = request
        return await self.get_match(request.match_id)

    async def get_match(self, match_id):
        if self.offline:
            raise ArenaError("UNAVAILABLE")
        if match_id not in self.requests:
            raise ArenaError("NOT_FOUND", 404)
        return ArenaStatus(match_id=match_id, run_id=self.requests[match_id].run_id, instance_id=self.instance,
            state="FAILED" if self.fail else "READY" if self.ready.is_set() else "PROVISIONING",
            endpoints=[Endpoint(name="ssh", host="192.0.2.2", port=30002), Endpoint(name="web", host="192.0.2.2", port=30003)],
            blue_password="temporary-password")

    async def destroy_match(self, match_id):
        self.requests.pop(match_id, None)

    async def get_events(self, match_id, after=0):
        self.observations += 1
        return EventPage(run_id=self.requests[match_id].run_id, instance_id=self.instance,
                         events=[], cursor=after)


def enter(client, count=4, prefix="pilot"):
    players = [client.post("/lobby/session", json={"nickname": f"{prefix}-{i}"}).json() for i in range(count)]
    for player in players:
        assert client.post("/lobby/queue", headers=auth(player["token"])).status_code == 200
    return players


def wait_status(client, player, expected, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        response = client.get("/lobby/session", headers=auth(player["token"]))
        assert response.status_code == 200
        state = response.json()
        if state.get("match", {}).get("state") == expected:
            return state
        assert time.monotonic() < deadline, state
        time.sleep(0.05)


def test_automatic_assignment_roles_restart_and_private_state(lobby_db):
    url, engine = lobby_db
    provider = FakeArena()
    with TestClient(create_app(url, arena_provider=provider)) as client:
        players = enter(client)
        states = [wait_status(client, p, "PROVISIONING") for p in players]
        assert len({s["match"]["id"] for s in states}) == 1
        assert sorted(s["match"]["team"] for s in states) == ["BLUE", "BLUE", "RED", "RED"]
        assert all(s["queued_players"] == 0 and s["status"] == "MATCHED" for s in states)
        for player, state in zip(players, states):
            assert len(state["match"]["allies"]) == 1
            ally = state["match"]["allies"][0]
            assert ally["id"] != player["player"]["id"]
            mate_state = next(s for s in states if s["player"]["id"] == ally["id"])
            assert mate_state["match"]["team"] == state["match"]["team"]
            for method, path in [("POST", "/lobby/queue"), ("DELETE", "/lobby/queue"), ("DELETE", "/lobby/session")]:
                assert client.request(method, path, headers=auth(player["token"])).status_code == 409
        with Session(engine) as session:
            match = session.scalar(select(Match))
            assert match.seed is not None and match.case_id == "web-001"
            assert match.red_key and match.blue_key and match.arena_run_id
            run_id = match.arena_run_id
            assert all(secret not in str(states) for secret in (match.red_key, match.blue_key, "temporary-password"))
    # Interrupted polling resumes the same persisted execution on Portal restart.
    provider.ready.set()
    with TestClient(create_app(url, arena_provider=provider)) as restarted:
        states = [wait_status(restarted, p, "RUNNING") for p in players]
        assert len({s["match"]["id"] for s in states}) == 1
        assert provider.observations > 0
    with Session(engine) as session:
        assert session.scalar(select(Match)).arena_run_id == run_id
        assert session.query(Match).count() == 1
        assert session.query(MatchPlayer).count() == 4


def test_atomic_claim_and_queue_cancellation(lobby_db):
    url, engine = lobby_db
    with TestClient(create_app(url)) as client:
        players = enter(client, 9)
        assert client.delete("/lobby/queue", headers=auth(players[0]["token"])).status_code == 200
        catalog = CaseCatalog(ROOT / "arena/cases")
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: claim_match(engine, catalog), range(4)))
        assert len([id for id in ids if id is not None]) == 2
        with Session(engine) as session:
            members = session.scalars(select(MatchPlayer)).all()
            assert len({m.player_id for m in members}) == 8
            assert players[0]["player"]["id"] not in {m.player_id for m in members}
            for match_id in filter(None, ids):
                assert sorted(m.team for m in members if m.match_id == match_id) == [Team.BLUE, Team.BLUE, Team.RED, Team.RED]


def test_no_ready_case_or_expired_players_stay_unassigned(lobby_db, tmp_path):
    from datetime import datetime, timedelta, timezone
    url, engine = lobby_db
    with TestClient(create_app(url)) as client:
        players = enter(client)
        assert claim_match(engine, CaseCatalog(tmp_path)) is None
        with Session(engine) as session:
            session.get(Player, players[0]["player"]["id"]).session_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            session.commit()
        assert claim_match(engine, CaseCatalog(ROOT / "arena/cases")) is None


def test_offline_retry_and_failed_match_can_requeue(lobby_db):
    url, engine = lobby_db
    provider = FakeArena()
    provider.offline = True
    with TestClient(create_app(url, arena_provider=provider)) as client:
        players = enter(client)
        wait_status(client, players[0], "PROVISIONING")
        # Let the uncertain create fail, then recover using the same persisted keys.
        deadline = time.monotonic() + 10
        while not provider.calls:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        with Session(engine) as session:
            match = session.scalar(select(Match))
            run_id, red_key = match.arena_run_id, match.red_key
        provider.offline = False
        provider.fail = True
        wait_status(client, players[0], "FAILED")
        assert client.post("/lobby/queue", headers=auth(players[0]["token"])).status_code == 200
        with Session(engine) as session:
            match = session.scalar(select(Match))
            assert match.arena_run_id == run_id and match.red_key is None
            assert red_key and not match.arena_cleanup_pending


def browser_app():
    provider = FakeArena()
    provider.ready.set()
    return create_app(arena_provider=provider)


@pytest.mark.skipif(os.getenv("RUN_BROWSER") != "1", reason="requires Playwright Chromium")
def test_four_browsers_receive_team_and_ally(lobby_db):
    from playwright.sync_api import sync_playwright, expect
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "--app-dir", str(ROOT / "tests"),
            "test_checkpoint_10:browser_app", "--factory", "--fd", str(listener.fileno())],
            pass_fds=(listener.fileno(),), cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        from urllib.request import urlopen
        deadline = time.monotonic() + 15
        while True:
            try:
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                    break
            except OSError:
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.1)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                pages = [browser.new_context().new_page() for _ in range(4)]
                for i, page in enumerate(pages):
                    page.goto(f"http://127.0.0.1:{port}/")
                    page.get_by_label("Nickname", exact=True).fill(f"browser-{i}")
                    page.get_by_role("button", name="Войти в лобби").click()
                    page.get_by_role("button", name="НАЧАТЬ ИГРУ").click()
                for page in pages:
                    expect(page.locator("#state-label")).to_have_text("RUNNING", timeout=20000)
                    expect(page.locator("#match-allies")).to_contain_text("browser-")
                    expect(page.locator("#play")).to_be_hidden()
                    expect(page.locator("#cancel")).to_be_hidden()
                assert sorted(p.locator("#match-team").inner_text() for p in pages) == ["BLUE", "BLUE", "RED", "RED"]
                assert len({p.locator("#match-id").inner_text() for p in pages}) == 1
                pages[0].reload()
                expect(pages[0].locator("#state-label")).to_have_text("RUNNING")
            finally:
                browser.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@pytest.mark.skipif(os.getenv("RUN_DOCKER") != "1", reason="requires real Docker")
def test_automatic_match_provisions_real_arena(lobby_db, monkeypatch):
    from arena.app.runtime.docker import DockerRuntime
    from arena.app.runtime.context import RuntimeMatch
    url, engine = lobby_db
    monkeypatch.setenv("ARENA_PROVIDER", "local")
    match_id = None
    with TestClient(create_app(url)) as client:
        players = enter(client)
        state = wait_status(client, players[0], "RUNNING", timeout=900)
        match_id = state["match"]["id"]
        states = [wait_status(client, player, "RUNNING") for player in players]
        assert sorted(s["match"]["team"] for s in states) == ["BLUE", "BLUE", "RED", "RED"]
        for player in players:
            response = client.get(f"/matches/{match_id}/access", headers=auth(player["token"]))
            assert response.status_code == 200
    runtime = DockerRuntime(ROOT / "arena/cases")
    assert runtime.inspect(RuntimeMatch(match_id)) is None
    for kind in ("container", "network", "volume"):
        assert runtime._resources(kind, str(match_id)) == []
