import json
import os
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.access import issue_player_token
from backend.app.cases import CaseLoader
from backend.app.database import Base, make_engine
from backend.app.main import create_app
from backend.app.models import Case, Match, MatchEvent, MatchPlayer, MatchState, Player, Team
from backend.app.provisioning import destroy_arena, provision_arena
from backend.app.runtime import DockerRuntime, DockerRuntimeError
from backend.app.victory import VictoryEngine


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def victory_arena(tmp_path):
    url = f"sqlite:///{tmp_path / 'victory.db'}"
    db = make_engine(url)
    Base.metadata.create_all(db)
    with Session(db) as session:
        session.add(Case(id="web-001", name="Test"))
        players = [Player(nickname=name) for name in ("red", "blue", "outsider")]
        tokens = [issue_player_token(player) for player in players]
        session.add_all(players)
        session.flush()
        match = Match(id=secrets.randbelow(2**52) + 1, case_id="web-001",
                      state=MatchState.RUNNING, target_host="127.0.0.1", blue_password="test-ssh",
                      red_key="RED_" + secrets.token_urlsafe(32), blue_key="BLUE_" + secrets.token_urlsafe(32))
        session.add(match)
        session.flush()
        session.add_all([MatchPlayer(match_id=match.id, player_id=players[0].id, team=Team.RED),
                         MatchPlayer(match_id=match.id, player_id=players[1].id, team=Team.BLUE)])
        session.commit()
        yield session, match, url, tokens
    db.dispose()


@pytest.fixture
def clocked(monkeypatch):
    clock = [0.0]
    probe = Mock(return_value={"blue_eligible": True})
    monkeypatch.setattr("backend.app.victory.monotonic", lambda: clock[0])
    monkeypatch.setattr("backend.app.victory._utcnow", lambda: datetime(2026, 1, 1) + timedelta(seconds=clock[0]))
    monkeypatch.setattr("backend.app.victory.check_red_defense", probe)
    return clock, probe


def timeline(session, match):
    return session.scalars(select(MatchEvent).where(MatchEvent.match_id == match.id).order_by(MatchEvent.id)).all()


def poll(engine, arena):
    session, match, _, _ = arena
    return engine.poll(session, match, CaseLoader().load(ROOT / "cases/web-001"))


def test_60_seconds_api_visibility_and_single_issue(victory_arena, clocked):
    session, match, url, tokens = victory_arena
    clock, probe = clocked
    engine = VictoryEngine(Mock(), ROOT / "cases")
    endpoint = f"/matches/{match.id}/access"
    headers = [{"Authorization": f"Bearer {token}"} for token in tokens]
    with TestClient(create_app(url)) as client:
        assert "blue_key" not in client.get(endpoint, headers=headers[1]).json()
        for second in (0, 20, 40, 59):
            clock[0] = second
            assert poll(engine, victory_arena) == {"status": "SECURING", "remaining_seconds": 60 - second}
            assert match.blue_key_issued_at is None
            assert "blue_key" not in client.get(endpoint, headers=headers[1]).json()
        clock[0] = 60
        assert poll(engine, victory_arena) == {"status": "BLUE_KEY_ISSUED"}
        assert client.get(endpoint, headers=headers[1]).json()["blue_key"] == match.blue_key
        red = client.get(endpoint, headers=headers[0])
        assert red.json() == {"host": match.target_host}
        assert match.red_key not in red.text and match.blue_key not in red.text
        assert client.get(endpoint, headers=headers[2]).status_code == 404
        count = probe.call_count
        assert poll(engine, victory_arena)["status"] == "BLUE_KEY_ISSUED"
        assert probe.call_count == count
        assert [event.type for event in timeline(session, match)] == [
            "BLUE_SECURING_STARTED", "BLUE_SECURED_TARGET", "BLUE_KEY_ISSUED",
        ]
        assert match.red_key not in str([event.event_metadata for event in timeline(session, match)])
        assert match.blue_key not in str([event.event_metadata for event in timeline(session, match)])
        destroy_arena(session, match, Mock())
        assert match.red_key is None and match.blue_key is None
        assert client.get(endpoint, headers=headers[1]).status_code == 409


@pytest.mark.parametrize("error", [False, True])
def test_failure_cancels_and_retry_requires_full_period(victory_arena, clocked, error):
    session, match, _, _ = victory_arena
    clock, probe = clocked
    engine = VictoryEngine(Mock(), ROOT / "cases")
    poll(engine, victory_arena)
    clock[0] = 20
    if error:
        probe.side_effect = DockerRuntimeError("failed")
    else:
        probe.return_value = {"blue_eligible": False}
    assert poll(engine, victory_arena)["status"] == "SECURING_CANCELLED"
    assert match.securing_started_at is None and match.blue_key_issued_at is None
    clock[0] = 21
    assert poll(engine, victory_arena)["status"] == "UNSECURED"
    assert [event.type for event in timeline(session, match)].count("BLUE_SECURING_CANCELLED") == 1
    probe.side_effect = None
    probe.return_value = {"blue_eligible": True}
    for second in (25, 45, 65, 84):
        clock[0] = second
        assert poll(engine, victory_arena)["status"] == "SECURING"
    clock[0] = 85
    assert poll(engine, victory_arena)["status"] == "BLUE_KEY_ISSUED"


@pytest.mark.parametrize("restart", [False, True])
def test_gap_or_restart_cannot_issue_from_old_timer(victory_arena, clocked, restart):
    session, match, _, _ = victory_arena
    clock, _ = clocked
    engine = VictoryEngine(Mock(), ROOT / "cases")
    poll(engine, victory_arena)
    clock[0] = 61
    if restart:
        engine = VictoryEngine(Mock(), ROOT / "cases")
    assert poll(engine, victory_arena) == {"status": "SECURING", "remaining_seconds": 60}
    assert match.blue_key_issued_at is None
    assert [e.type for e in timeline(session, match)] == [
        "BLUE_SECURING_STARTED", "BLUE_SECURING_CANCELLED", "BLUE_SECURING_STARTED",
    ]


def test_slow_probe_and_wall_clock_jump_do_not_skip_stabilization(victory_arena, clocked, monkeypatch):
    _, match, _, _ = victory_arena
    clock, probe = clocked
    engine = VictoryEngine(Mock(), ROOT / "cases")
    poll(engine, victory_arena)
    monkeypatch.setattr("backend.app.victory._utcnow", lambda: datetime(2036, 1, 1))
    clock[0] = 10
    assert poll(engine, victory_arena)["remaining_seconds"] == 50

    def slow(*args):
        clock[0] += 31
        return {"blue_eligible": True}

    probe.side_effect = slow
    assert poll(engine, victory_arena)["status"] == "SECURING_CANCELLED"
    assert match.blue_key_issued_at is None


def test_stopped_match_never_issues_and_old_matches_require_new_keys(victory_arena, clocked):
    session, match, _, _ = victory_arena
    _, probe = clocked
    engine = VictoryEngine(Mock(), ROOT / "cases")
    match.red_key = None
    session.commit()
    with pytest.raises(ValueError, match="no keys"):
        poll(engine, victory_arena)
    match.state = MatchState.FINISHED
    session.commit()
    assert poll(engine, victory_arena)["status"] == "STOPPED"
    probe.assert_not_called()


@pytest.mark.skipif(os.getenv("RUN_DOCKER") != "1", reason="requires real Docker and 60-second stabilization")
def test_real_keys_cancellation_firewall_and_60_second_stabilization(victory_arena):
    session, match, url, tokens = victory_arena
    case = CaseLoader().load(ROOT / "cases/web-001")
    assert case.blue.stabilization_seconds == 60
    match.state = MatchState.PROVISIONING
    match.target_host = None
    session.commit()
    runtime = DockerRuntime(ROOT / "cases")
    engine = VictoryEngine(runtime, ROOT / "cases")

    def wait_securing():
        deadline = time.monotonic() + 30
        while True:
            result = poll(engine, victory_arena)
            if result["status"] == "SECURING":
                return result
            assert time.monotonic() < deadline, result
            time.sleep(0.2)

    def patch(target, enable):
        source, replacement = ('elif url.path == "/download":', 'elif False:') if enable else ('elif False:', 'elif url.path == "/download":')
        runtime._docker(
            "exec", target["Id"], "python3", "-c",
            "from pathlib import Path; import sys; p=Path('/opt/service/server.py'); "
            "s=p.read_text(); assert sys.argv[1] in s; p.write_text(s.replace(sys.argv[1],sys.argv[2]))",
            source, replacement,
        )
        runtime._docker("exec", target["Id"], "service", "cyberrange-web", "restart")

    try:
        provision_arena(session, match, case, runtime)
        target = runtime.inspect(match)
        assert runtime._docker("exec", target["Id"], "cat", case.red.objective_path) == match.red_key
        assert runtime._docker("exec", target["Id"], "ls", "-A", "/opt/objective") == "red-key"
        assert match.blue_key not in json.dumps(target)
        assert poll(engine, victory_arena)["status"] == "UNSECURED"
        patch(target, True)
        wait_securing()
        patch(target, False)
        assert poll(engine, victory_arena)["status"] == "SECURING_CANCELLED"
        assert match.blue_key_issued_at is None
        patch(target, True)
        wait_securing()
        network_id = next(iter(target["NetworkSettings"]["Networks"].values()))["NetworkID"]
        network = json.loads(runtime._docker("network", "inspect", network_id))[0]
        ipam = network["IPAM"]["Config"][0]
        runtime._docker("exec", target["Id"], "iptables", "-I", "INPUT", "1", "-p", "tcp",
                        "-s", ipam["Gateway"], "--dport", "80", "-j", "ACCEPT")
        runtime._docker("exec", target["Id"], "iptables", "-I", "INPUT", "2", "-p", "tcp",
                        "-s", ipam["Subnet"], "--dport", "80", "-j", "DROP")
        assert poll(engine, victory_arena)["status"] == "SECURING_CANCELLED"
        assert match.blue_key_issued_at is None
        runtime._docker("exec", target["Id"], "iptables", "-F", "INPUT")
        started = time.monotonic()
        wait_securing()
        deadline = started + 150
        endpoint = f"/matches/{match.id}/access"
        with TestClient(create_app(url)) as client:
            headers = [{"Authorization": f"Bearer {token}"} for token in tokens]
            while True:
                assert "blue_key" not in client.get(endpoint, headers=headers[1]).json()
                time.sleep(2)
                result = poll(engine, victory_arena)
                if result["status"] == "BLUE_KEY_ISSUED":
                    break
                assert time.monotonic() < deadline, result
            assert time.monotonic() - started >= 60
            assert client.get(endpoint, headers=headers[1]).json()["blue_key"] == match.blue_key
            assert client.get(endpoint, headers=headers[0]).json() == {"host": match.target_host}
            assert match.state == MatchState.RUNNING
        assert [e.type for e in timeline(session, match)].count("BLUE_KEY_ISSUED") == 1
    finally:
        destroy_arena(session, match, runtime)
