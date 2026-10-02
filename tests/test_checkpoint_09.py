import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from portal.app.database import make_engine
from portal.app.main import create_app
from portal.app.models import Match, MatchPlayer, Player


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def lobby_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'lobby.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    engine = make_engine(url)
    yield url, engine
    engine.dispose()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_four_players_queue_refresh_leave_and_no_matchmaking(lobby_db):
    url, engine = lobby_db
    with TestClient(create_app(url)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        players = [client.post("/lobby/session", json={"nickname": f"player-{i}"}).json() for i in range(4)]
        assert len({p["token"] for p in players}) == 4
        for i, player in enumerate(players):
            response = client.post("/lobby/queue", headers=auth(player["token"]))
            assert response.status_code == 200
            assert response.json()["status"] == "SEARCHING"
            assert response.json()["queued_players"] == i + 1
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(Match)) == 0
            before = session.get(Player, players[0]["player"]["id"]).queued_at
        client.post("/lobby/queue", headers=auth(players[0]["token"]))
        with Session(engine) as session:
            assert session.get(Player, players[0]["player"]["id"]).queued_at == before
    with TestClient(create_app(url)) as restarted:
        for player in players:
            response = restarted.get("/lobby/session", headers=auth(player["token"]))
            assert response.json()["status"] == "SEARCHING"
            assert response.json()["queued_players"] == 4
            assert "token" not in response.json()
            assert response.headers["cache-control"] == "no-store"
        for player in players:
            response = restarted.delete("/lobby/queue", headers=auth(player["token"]))
            assert response.json()["status"] == "IDLE"
        assert response.json()["queued_players"] == 0


@pytest.mark.parametrize("nickname", ["", "a", "a" * 25, "has space", "<script>", "foo\nbar"])
def test_nickname_validation(lobby_db, nickname):
    with TestClient(create_app(lobby_db[0])) as client:
        assert client.post("/lobby/session", json={"nickname": nickname}).status_code == 422


def test_duplicate_names_logout_expiry_and_historical_identity(lobby_db):
    url, engine = lobby_db
    with TestClient(create_app(url)) as client:
        first = client.post("/lobby/session", json={"nickname": "  Pilot  "}).json()
        assert first["player"]["nickname"] == "Pilot"
        assert client.post("/lobby/session", json={"nickname": "ＰＩＬＯＴ"}).status_code == 409
        assert client.delete("/lobby/session", headers=auth(first["token"])).status_code == 200
        second = client.post("/lobby/session", json={"nickname": "Pilot"}).json()
        assert second["player"]["id"] != first["player"]["id"]
        assert client.get("/lobby/session", headers=auth(first["token"])).status_code == 401
        client.post("/lobby/queue", headers=auth(second["token"]))
        with Session(engine) as session:
            player = session.get(Player, second["player"]["id"])
            player.session_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            session.commit()
        assert client.get("/lobby/session", headers=auth(second["token"])).status_code == 401
        third = client.post("/lobby/session", json={"nickname": "Pilot"}).json()
        assert third["queued_players"] == 0
        assert third["player"]["id"] != second["player"]["id"]
        with Session(engine) as session:
            assert session.get(Player, first["player"]["id"]).nickname == "Pilot"
            assert session.get(Player, second["player"]["id"]).queued_at is None


def test_racing_registration_and_token_boundary(lobby_db):
    with TestClient(create_app(lobby_db[0])) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.post("/lobby/session", json={"nickname": "same"}), range(2)))
        assert sorted(response.status_code for response in responses) == [201, 409]
        first = next(response.json() for response in responses if response.status_code == 201)
        second = client.post("/lobby/session", json={"nickname": "other"}).json()
        for method, path in [("GET", "/lobby/session"), ("POST", "/lobby/queue"),
                             ("DELETE", "/lobby/queue"), ("DELETE", "/lobby/session")]:
            assert client.request(method, path).status_code == 401
            assert client.request(method, path, headers=auth("wrong")).status_code == 401
        client.post(f"/lobby/queue?player_id={second['player']['id']}", headers=auth(first["token"]))
        assert client.get("/lobby/session", headers=auth(second["token"])).json()["status"] == "IDLE"
        assert client.post("/lobby/session", json={"nickname": "new", "team": "BLUE"}).status_code == 422


def test_migration_preserves_match_memberships(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "0003")
    engine = make_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO players (id, nickname) VALUES (1, 'old')"))
            connection.execute(text("INSERT INTO matches (id) VALUES (1)"))
            connection.execute(text("INSERT INTO match_players (match_id, player_id, team) VALUES (1, 1, 'BLUE')"))
        command.upgrade(config, "head")
        command.check(config)
        with Session(engine) as session:
            assert session.get(Player, 1).nickname == "old"
            assert session.get(MatchPlayer, (1, 1)).team == "BLUE"
        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        engine.dispose()


@pytest.mark.skipif(os.getenv("RUN_BROWSER") != "1", reason="set RUN_BROWSER=1 with Playwright Chromium installed")
def test_four_browser_sessions(lobby_db, tmp_path):
    from playwright.sync_api import sync_playwright, expect

    # Pass a reserved socket to Uvicorn, avoiding a port-selection race.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "portal.app.main:app", "--fd", str(listener.fileno())],
            cwd=ROOT, pass_fds=(listener.fileno(),), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                with urlopen(url + "/health", timeout=1) as response:
                    assert response.status == 200
                break
            except (URLError, OSError):
                assert process.poll() is None and time.monotonic() < deadline, "test server failed to start"
                time.sleep(0.1)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            contexts = [browser.new_context(viewport={"width": 1280, "height": 900}) for _ in range(5)]
            errors = []
            pages = [context.new_page() for context in contexts]
            try:
                for i, page in enumerate(pages[:4]):
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(url)
                    if i == 0:
                        page.screenshot(path=tmp_path / "lobby.png", full_page=True)
                    page.get_by_label("Nickname", exact=True).fill(f"pilot-{i}")
                    page.get_by_role("button", name="Войти в лобби").click()
                    page.get_by_role("button", name="НАЧАТЬ ИГРУ").click()
                    expect(page.get_by_text("SEARCHING FOR MATCH", exact=True)).to_be_visible()
                pages[0].reload()
                for page in pages[:4]:
                    expect(page.get_by_text("SEARCHING FOR MATCH", exact=True)).to_be_visible()
                    expect(page.get_by_text("Игроков в очереди: 4", exact=True)).to_be_visible(timeout=12000)
                pages[4].goto(url)
                pages[4].get_by_label("Nickname", exact=True).fill("PILOT-0")
                pages[4].get_by_role("button", name="Войти в лобби").click()
                expect(pages[4].get_by_role("alert")).to_contain_text("уже занят")
                for page in pages[:4]:
                    page.get_by_role("button", name="Выйти из очереди", exact=True).click()
                    expect(page.get_by_text("READY", exact=True)).to_be_visible()
                expect(pages[0].get_by_text("Игроков в очереди: 0", exact=True)).to_be_visible(timeout=12000)
                pages[0].get_by_role("button", name="Сменить nickname").click()
                expect(pages[0].get_by_label("Nickname", exact=True)).to_be_visible()
                pages[4].get_by_role("button", name="Войти в лобби").click()
                expect(pages[4].get_by_text("READY", exact=True)).to_be_visible()
                pages[0].set_viewport_size({"width": 360, "height": 800})
                assert pages[0].evaluate("document.documentElement.scrollWidth <= window.innerWidth")
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
