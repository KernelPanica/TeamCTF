import os
import asyncio
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import Mock

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from portal.app.cases import CaseLoader
from portal.app.database import Base, make_engine
from arena_support import HealthChecker
from portal.app.health import watch_health
from arena_support import observation_provider
from portal.app.models import Case, Match, MatchEvent, MatchState
from arena_support import destroy_arena, provision_arena
from arena.app.runtime.docker import DockerRuntime


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def health_arena(tmp_path):
    directory = tmp_path / "cases" / "web-001"
    shutil.copytree(ROOT / "arena/cases/web-001", directory)
    engine = make_engine(f"sqlite:///{tmp_path / 'health.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Case(id="web-001", name="Test"))
        session.flush()
        match = Match(id=secrets.randbelow(2**52) + 1, case_id="web-001",
                      state=MatchState.RUNNING, target_host="127.0.0.1")
        session.add(match)
        session.commit()
        yield session, match, directory
    engine.dispose()


def events(session, match):
    return session.scalars(select(MatchEvent).where(MatchEvent.match_id == match.id).order_by(MatchEvent.id)).all()


def test_transitions_downtime_and_restart(health_arena, monkeypatch):
    session, match, directory = health_arena
    checker = HealthChecker(directory.parent)
    case = CaseLoader().load(directory)
    base = datetime(2026, 1, 1)
    clock = iter(base + timedelta(seconds=offset) for offset in [0, 2, 4, 6, 9, 11, 14, 18])
    monkeypatch.setattr("portal.app.health._utcnow", lambda: next(clock))
    run = Mock(side_effect=[subprocess.CompletedProcess([], code) for code in [0, 0, 1, 1, 0, 0, 1, 0]])
    monkeypatch.setattr(subprocess, "run", run)
    results = []
    for _ in range(8):
        # State comes from persisted events, not memory in the checker.
        checker = HealthChecker(directory.parent)
        results.append(checker.poll(session, match, case))
    assert [r["services"]["web"]["downtime_seconds"] for r in results] == [0, 0, 0, 2, 5, 5, 5, 9]
    timeline = events(session, match)
    assert [event.type for event in timeline] == ["SERVICE_UP", "SERVICE_DOWN", "SERVICE_RESTORED", "SERVICE_DOWN", "SERVICE_RESTORED"]
    assert timeline[-1].event_metadata["downtime_seconds"] == 9
    assert run.call_args.args[0][:2] == [sys.executable, str(directory / "checks/health.py")]
    assert run.call_args.kwargs["timeout"] == 5


def test_initial_failure_and_multiple_services(health_arena, monkeypatch):
    session, match, directory = health_arena
    manifest = directory / "case.yaml"
    data = yaml.safe_load(manifest.read_text())
    data["required_services"].append({"name": "other", "port": 8080, "protocol": "tcp"})
    manifest.write_text(yaml.safe_dump(data))
    run = Mock(side_effect=[subprocess.CompletedProcess([], code) for code in [1, 0, 1, 0, 0, 0]])
    monkeypatch.setattr(subprocess, "run", run)
    case = CaseLoader().load(directory)
    checker = HealthChecker(directory.parent)
    assert checker.poll(session, match, case)["status"] == "UNHEALTHY"
    assert checker.poll(session, match, case)["status"] == "UNHEALTHY"
    assert checker.poll(session, match, case)["status"] == "HEALTHY"
    assert [(e.type, e.event_metadata["service"]) for e in events(session, match)] == [
        ("SERVICE_DOWN", "web"), ("SERVICE_UP", "other"), ("SERVICE_RESTORED", "web"),
    ]
    assert run.call_args.args[0][-3:] == ["http://127.0.0.1:8080", "other", "tcp"]


@pytest.mark.parametrize("script,reason", [
    ("import time; time.sleep(30)", "TIMEOUT"),
    ("raise RuntimeError('broken checker')", "CHECK_FAILED"),
    ("raise SystemExit(2)", "CHECK_ERROR"),
])
def test_checker_timeout_and_failure(health_arena, script, reason):
    session, match, directory = health_arena
    (directory / "checks/health.py").write_text(script)
    checker = HealthChecker(directory.parent, timeout=0.5)
    result = checker.poll(session, match, CaseLoader().load(directory))
    assert result["status"] == "UNHEALTHY"
    assert result["services"]["web"]["reason"] == reason
    assert events(session, match)[0].type == "SERVICE_DOWN"


def test_functional_contract_not_just_open_port(health_arena):
    session, match, directory = health_arena

    class Handler(BaseHTTPRequestHandler):
        body = b"wrong response"

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(self.body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manifest = directory / "case.yaml"
    data = yaml.safe_load(manifest.read_text())
    data["required_services"][0]["port"] = server.server_port
    manifest.write_text(yaml.safe_dump(data))
    checker = HealthChecker(directory.parent)
    case = CaseLoader().load(directory)
    try:
        assert checker.poll(session, match, case)["status"] == "UNHEALTHY"
        Handler.body = b"Cyber Range file service\n"
        assert checker.poll(session, match, case)["status"] == "HEALTHY"
        assert [event.type for event in events(session, match)] == ["SERVICE_DOWN", "SERVICE_RESTORED"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_watcher_stops_after_arena_cleanup(health_arena, monkeypatch):
    session, match, directory = health_arena
    monkeypatch.setattr(subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0)))
    provider = observation_provider(session, match, CaseLoader().load(directory), "test-key", directory.parent)
    watcher = watch_health(session.get_bind(), match.id, provider, interval=0.001)
    with asyncio.Runner() as runner:
        assert runner.run(anext(watcher))["status"] == "HEALTHY"
        match.target_host = None
        session.commit()
        with pytest.raises(StopAsyncIteration):
            runner.run(anext(watcher))
    assert len([e for e in events(session, match) if e.type == "SERVICE_UP"]) == 1


def test_stopped_or_wrong_case_not_checked(health_arena, monkeypatch):
    session, match, directory = health_arena
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    case = CaseLoader().load(directory)
    checker = HealthChecker(directory.parent)
    with pytest.raises(ValueError, match="match case"):
        checker.poll(session, match, case.model_copy(update={"id": "different"}))
    match.state = MatchState.FINISHED
    with pytest.raises(ValueError, match="running arena"):
        checker.poll(session, match, case)
    run.assert_not_called()


@pytest.mark.skipif(os.getenv("RUN_DOCKER") != "1", reason="requires real Docker; set RUN_DOCKER=1")
def test_real_service_stop_restore_and_downtime(health_arena):
    session, match, _ = health_arena
    match.state = MatchState.PROVISIONING
    match.target_host = None
    session.commit()
    runtime = DockerRuntime(ROOT / "arena/cases")
    case = CaseLoader().load(ROOT / "arena/cases/web-001")
    checker = HealthChecker(ROOT / "arena/cases")

    def wait_healthy():
        deadline = time.monotonic() + 10
        while True:
            result = checker.poll(session, match, case)
            if result["status"] == "HEALTHY":
                return result
            assert time.monotonic() < deadline, "web did not recover"
            time.sleep(0.1)

    try:
        provision_arena(session, match, case, runtime)
        wait_healthy()
        target = runtime.inspect(match)
        baseline = len(events(session, match))
        checker.poll(session, match, case)
        assert len(events(session, match)) == baseline
        runtime._docker("exec", target["Id"], "service", "cyberrange-web", "stop")
        assert checker.poll(session, match, case)["status"] == "UNHEALTHY"
        down = events(session, match)[-1]
        assert down.type == "SERVICE_DOWN"
        time.sleep(0.2)
        result = checker.poll(session, match, case)
        assert result["services"]["web"]["downtime_seconds"] > 0
        assert events(session, match)[-1].id == down.id
        runtime._docker("exec", target["Id"], "service", "cyberrange-web", "start")
        result = wait_healthy()
        restored = events(session, match)[-1]
        assert restored.type == "SERVICE_RESTORED"
        expected = down.event_metadata["downtime_seconds"] + (restored.timestamp - down.timestamp).total_seconds()
        assert result["services"]["web"]["downtime_seconds"] == pytest.approx(expected)
        assert checker.poll(session, match, case)["services"]["web"]["downtime_seconds"] == pytest.approx(expected)
    finally:
        destroy_arena(session, match, runtime)
