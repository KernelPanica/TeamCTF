import json
import os
import secrets
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from portal.app.cases import CaseLoader
from portal.app.database import Base, make_engine
from portal.app.models import Case, Match, MatchState
from arena_support import destroy_arena, provision_arena
from arena.app.checks.red_zone import RedZoneChecker
from arena_support import check_red_defense
from arena.app.runtime.docker import DockerRuntime, DockerRuntimeError


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def red_arena(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'red-zone.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Case(id="web-001", name="Test"))
        session.flush()
        match = Match(
            id=secrets.randbelow(2**52) + 1, state=MatchState.RUNNING,
            case_id="web-001", target_host="172.20.0.2",
        )
        session.add(match)
        session.commit()
        yield session, match
    engine.dispose()


def target():
    return {
        "Id": "target-id",
        "Image": "sha256:" + "a" * 64,
        "Name": "/range-match-42-target",
        "State": {"Running": True},
        "NetworkSettings": {"Networks": {"match": {"NetworkID": "network-id"}}},
    }


@pytest.mark.parametrize(
    "health_code,exploit_code,surface,exploit",
    [(0, 0, "AVAILABLE", "VULNERABLE"), (0, 10, "AVAILABLE", "PATCHED"),
     (1, 11, "UNAVAILABLE", "UNREACHABLE"), (2, 2, "UNAVAILABLE", "ERROR")],
)
def test_probe_contract_and_cleanup(red_arena, monkeypatch, health_code, exploit_code,
                                    surface, exploit):
    _, match = red_arena
    runtime = Mock()
    runtime.inspect.return_value = target()
    calls = []

    def docker(*args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "build":
            dockerfile = Path(args[args.index("--file") + 1]).read_text()
            assert "COPY checks/ /opt/checks/" in dockerfile
            assert "rm -rf /opt/objective" in dockerfile
            return ""
        if args[0] == "create":
            return "probe-id"
        if args[0] == "exec" and args[2:5] == ("test", "!", "-e"):
            return ""
        if args[0] == "exec":
            return str(health_code if any(arg.endswith("health.py") for arg in args) else exploit_code)
        return ""

    runtime._docker.side_effect = docker
    checker = RedZoneChecker(runtime, ROOT / "arena/cases")
    monkeypatch.setattr("arena.app.checks.red_zone.secrets.token_hex", lambda _: "12345678")
    result = checker.check(match, CaseLoader().load(ROOT / "arena/cases/web-001"), "secret")
    assert result["surface"] == surface and result["exploit"] == exploit
    create = next(args for args, _ in calls if args[0] == "create")
    assert "--read-only" in create and create[create.index("--cap-drop") + 1] == "ALL"
    assert create[create.index("--security-opt") + 1] == "no-new-privileges"
    assert create[create.index("--network") + 1] == "network-id"
    assert "match_id=" + str(match.id) in create and "role=red-probe" in create
    exploit_call = next((args, kwargs) for args, kwargs in calls
                        if args[0] == "exec" and any(arg.endswith("exploit.py") for arg in args))
    assert exploit_call[1]["stdin"] == "secret"
    assert "secret" not in str(exploit_call[0])
    assert calls[-1][0] == ("container", "rm", "--force", "--volumes", "probe-id")


def test_probe_cleanup_after_checker_error(red_arena):
    _, match = red_arena
    runtime = Mock()
    runtime.inspect.return_value = target()

    def docker(*args, **kwargs):
        if args[0] == "create":
            return "probe-id"
        if args[0] == "exec" and any(arg.endswith("health.py") for arg in args):
            raise DockerRuntimeError("probe failed")
        return ""

    runtime._docker.side_effect = docker
    with pytest.raises(DockerRuntimeError, match="probe failed"):
        RedZoneChecker(runtime, ROOT / "arena/cases").check(
            match, CaseLoader().load(ROOT / "arena/cases/web-001"), "secret"
        )
    runtime._docker.assert_called_with("container", "rm", "--force", "--volumes", "probe-id")


def test_invalid_target_topology(red_arena):
    _, match = red_arena
    runtime = Mock()
    broken = target()
    broken["NetworkSettings"]["Networks"]["other"] = {"NetworkID": "other"}
    runtime.inspect.return_value = broken
    with pytest.raises(DockerRuntimeError, match="exactly one"):
        RedZoneChecker(runtime, ROOT / "arena/cases").check(
            match, CaseLoader().load(ROOT / "arena/cases/web-001"), "secret"
        )
    runtime._docker.assert_not_called()


@pytest.mark.skipif(os.getenv("RUN_DOCKER") != "1", reason="requires real Docker; set RUN_DOCKER=1")
def test_stop_firewall_and_valid_patch(red_arena):
    session, match = red_arena
    match.state = MatchState.PROVISIONING
    match.target_host = None
    session.commit()
    runtime = DockerRuntime(ROOT / "arena/cases")
    case = CaseLoader().load(ROOT / "arena/cases/web-001")

    def wait_for(expected_surface=None):
        deadline = time.monotonic() + 15
        while True:
            result = check_red_defense(
                session, match, case, match.red_key, runtime, ROOT / "arena/cases"
            )
            if result["health"]["status"] == "HEALTHY" and (
                expected_surface is None or result["red_zone"]["surface"] == expected_surface
            ):
                return result
            assert time.monotonic() < deadline, result
            time.sleep(0.1)

    try:
        provision_arena(session, match, case, runtime)
        target_data = runtime.inspect(match)
        result = wait_for("AVAILABLE")
        assert result["red_zone"]["exploit"] == "VULNERABLE"
        assert result["blue_eligible"] is False

        # A: stopping the required service fails both contracts.
        runtime._docker("exec", target_data["Id"], "service", "cyberrange-web", "stop")
        result = check_red_defense(
            session, match, case, match.red_key, runtime, ROOT / "arena/cases"
        )
        assert result["health"]["status"] == "UNHEALTHY"
        assert result["red_zone"]["surface"] == "UNAVAILABLE"
        assert result["blue_eligible"] is False
        runtime._docker("exec", target_data["Id"], "service", "cyberrange-web", "start")
        wait_for("AVAILABLE")

        # B: controller health remains reachable, but peers in the RED subnet are blocked.
        network_id = next(iter(target_data["NetworkSettings"]["Networks"].values()))["NetworkID"]
        network = json.loads(runtime._docker("network", "inspect", network_id))[0]
        ipam = network["IPAM"]["Config"][0]
        runtime._docker(
            "exec", target_data["Id"], "iptables", "-I", "INPUT", "1", "-p", "tcp",
            "-s", ipam["Gateway"], "--dport", "80", "-j", "ACCEPT",
        )
        runtime._docker(
            "exec", target_data["Id"], "iptables", "-I", "INPUT", "2", "-p", "tcp",
            "-s", ipam["Subnet"], "--dport", "80", "-j", "DROP",
        )
        result = check_red_defense(
            session, match, case, match.red_key, runtime, ROOT / "arena/cases"
        )
        assert result["health"]["status"] == "HEALTHY"
        assert result["red_zone"]["surface"] == "UNAVAILABLE"
        assert result["blue_eligible"] is False
        runtime._docker("exec", target_data["Id"], "iptables", "-F", "INPUT")

        # C: remove only the unsafe route; required functionality remains available.
        runtime._docker(
            "exec", target_data["Id"], "python3", "-c",
            "from pathlib import Path; p=Path('/opt/service/server.py'); "
            "s=p.read_text(); assert 'elif url.path == \\\"/download\\\":' in s; "
            "p.write_text(s.replace('elif url.path == \\\"/download\\\":', 'elif False:'))",
        )
        runtime._docker("exec", target_data["Id"], "service", "cyberrange-web", "restart")
        result = wait_for("AVAILABLE")
        assert result["red_zone"]["exploit"] == "PATCHED"
        assert result["blue_eligible"] is True
    finally:
        destroy_arena(session, match, runtime)
