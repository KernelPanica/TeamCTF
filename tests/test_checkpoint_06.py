import os
import secrets
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import Mock

import pytest
import yaml
from sqlalchemy.orm import Session

from portal.app.cases import CaseLoader
from portal.app.database import Base, make_engine
from arena.app.checks.exploit import ExploitChecker
from arena_support import check_defense
from portal.app.models import Case, Match, MatchState
from arena_support import destroy_arena, provision_arena
from arena.app.runtime.docker import DockerRuntime


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def exploit_arena(tmp_path):
    directory = tmp_path / "cases/web-001"
    shutil.copytree(ROOT / "arena/cases/web-001", directory)
    engine = make_engine(f"sqlite:///{tmp_path / 'exploit.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Case(id="web-001", name="Test"))
        session.flush()
        match = Match(id=secrets.randbelow(2**52) + 1, state=MatchState.RUNNING,
                      case_id="web-001", target_host="127.0.0.1")
        session.add(match)
        session.commit()
        yield session, match, directory
    engine.dispose()


@pytest.mark.parametrize("script,expected", [
    ("raise SystemExit(0)", "VULNERABLE"),
    ("raise SystemExit(10)", "PATCHED"),
    ("raise SystemExit(11)", "UNREACHABLE"),
    ("raise RuntimeError('crashed')", "ERROR"),
    ("raise SystemExit(2)", "ERROR"),
    ("import time; time.sleep(30)", "ERROR"),
])
def test_exit_contract_and_timeout(exploit_arena, script, expected):
    _, match, directory = exploit_arena
    (directory / "checks/exploit.py").write_text(script)
    checker = ExploitChecker(directory.parent, timeout=0.5)
    assert checker.check(match, CaseLoader().load(directory), "test-key") == expected


def test_key_only_in_stdin_and_launch_errors(exploit_arena, monkeypatch):
    _, match, directory = exploit_arena
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess, "run", run)
    checker = ExploitChecker(directory.parent)
    case = CaseLoader().load(directory)
    assert checker.check(match, case, "private-objective") == "VULNERABLE"
    assert run.call_args.kwargs["input"] == "private-objective"
    assert "private-objective" not in str(run.call_args.args)
    run.side_effect = OSError("unavailable")
    assert checker.check(match, case, "private-objective") == "ERROR"


def test_rejects_wrong_case_and_inactive_match(exploit_arena, monkeypatch):
    _, match, directory = exploit_arena
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    checker = ExploitChecker(directory.parent)
    case = CaseLoader().load(directory)
    with pytest.raises(ValueError, match="match case"):
        checker.check(match, case.model_copy(update={"id": "other"}), "key")
    with pytest.raises(ValueError, match="objective"):
        checker.check(match, case, "")
    match.state = MatchState.FINISHED
    with pytest.raises(ValueError, match="running arena"):
        checker.check(match, case, "key")
    run.assert_not_called()


def test_network_probe_patches_errors_and_service_down(exploit_arena):
    session, match, directory = exploit_arena

    class Handler(BaseHTTPRequestHandler):
        mode = "vulnerable"

        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Cyber Range file service\n")
                return
            if self.mode in ("forbidden", "removed", "error"):
                self.send_error({"forbidden": 403, "removed": 404, "error": 500}[self.mode])
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"test-key" if self.mode == "vulnerable" else b"not the objective")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manifest = directory / "case.yaml"
    data = yaml.safe_load(manifest.read_text())
    data["required_services"][0]["port"] = server.server_port
    manifest.write_text(yaml.safe_dump(data))
    case = CaseLoader().load(directory)
    try:
        for mode, expected, eligible in [
            ("vulnerable", "VULNERABLE", False),
            ("forbidden", "PATCHED", True),
            ("removed", "PATCHED", True),
            ("filtered", "PATCHED", True),
            ("error", "ERROR", False),
            ("vulnerable", "VULNERABLE", False),
        ]:
            Handler.mode = mode
            result = check_defense(session, match, case, "test-key", directory.parent)
            assert result["health"]["status"] == "HEALTHY"
            assert result["exploit"] == expected
            assert result["blue_eligible"] is eligible
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    result = check_defense(session, match, case, "test-key", directory.parent)
    assert result["exploit"] == "UNREACHABLE"
    assert result["health"]["status"] == "UNHEALTHY"
    assert result["blue_eligible"] is False


@pytest.mark.skipif(os.getenv("RUN_DOCKER") != "1", reason="requires real Docker; set RUN_DOCKER=1")
def test_real_target_vulnerable_patched_and_stopped(exploit_arena):
    session, match, _ = exploit_arena
    match.state = MatchState.PROVISIONING
    match.target_host = None
    session.commit()
    runtime = DockerRuntime(ROOT / "arena/cases")
    case = CaseLoader().load(ROOT / "arena/cases/web-001")

    def check_ready():
        deadline = time.monotonic() + 10
        while True:
            result = check_defense(session, match, case, match.red_key, ROOT / "arena/cases")
            if result["health"]["status"] == "HEALTHY":
                return result
            assert time.monotonic() < deadline, "web did not become healthy"
            time.sleep(0.1)

    try:
        provision_arena(session, match, case, runtime)
        result = check_ready()
        assert result["exploit"] == "VULNERABLE" and result["blue_eligible"] is False
        target = runtime.inspect(match)
        # This case only requires GET /: removing the unsafe download route is a valid repair.
        runtime._docker("exec", target["Id"], "python3", "-c",
                        "from pathlib import Path; p=Path('/opt/service/server.py'); "
                        "s=p.read_text(); assert 'elif url.path == \"/download\":' in s; "
                        "p.write_text(s.replace('elif url.path == \"/download\":', 'elif False:'))")
        runtime._docker("exec", target["Id"], "service", "cyberrange-web", "restart")
        result = check_ready()
        assert result["exploit"] == "PATCHED" and result["blue_eligible"] is True
        runtime._docker("exec", target["Id"], "service", "cyberrange-web", "stop")
        result = check_defense(session, match, case, match.red_key, ROOT / "arena/cases")
        assert result["exploit"] == "UNREACHABLE"
        assert result["health"]["status"] == "UNHEALTHY" and result["blue_eligible"] is False
    finally:
        destroy_arena(session, match, runtime)
