import json
import os
import secrets
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from backend.app.cases import CaseLoader
from backend.app.database import Base, make_engine
from backend.app.models import Match
from backend.app.runtime import DockerRuntime, DockerRuntimeError


CASE_ROOT = Path(__file__).resolve().parents[1] / "cases"


@pytest.mark.parametrize("failure", ["create", "inspect", "volumes"])
def test_prepare_limits_and_rollback(monkeypatch, failure):
    runtime = DockerRuntime(CASE_ROOT)
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[1] == "ls":
            return ""
        if args[0] == "build":
            Path(args[args.index("--iidfile") + 1]).write_text("sha256:test-image")
            return ""
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Config": {"Volumes": {"/data": {}} if failure == "volumes" else None}}])
        if args[:2] == ("network", "create"):
            return "new-network-id"
        if args[0] == "create":
            if failure == "create":
                raise DockerRuntimeError("simulated create failure")
            return "new-container-id"
        if args[1] == "rm":
            return ""
        pytest.fail(f"unexpected Docker call: {args}")

    monkeypatch.setattr(runtime, "_docker", docker)
    with pytest.raises(DockerRuntimeError):
        runtime.prepare(Match(id=1), CaseLoader().load(CASE_ROOT / "web-001"))
    if failure == "volumes":
        assert not any(call[0] == "create" or call[:2] == ("network", "create") for call in calls)
        return
    create = next(call for call in calls if call[0] == "create")
    assert create[create.index("--cpus") + 1] == "1"
    assert create[create.index("--memory") + 1] == "256m"
    assert create[create.index("--pids-limit") + 1] == "128"
    assert "--privileged" not in create and "--pid" not in create
    assert "--volume" not in create and "--mount" not in create
    assert create[create.index("--network") + 1] == "new-network-id"
    assert calls[-1] == ("network", "rm", "new-network-id")
    if failure == "inspect":
        assert calls[-2] == ("container", "rm", "--force", "--volumes", "new-container-id")
    network = next(call for call in calls if call[:2] == ("network", "create"))
    assert "--internal" in network and "match_id=1" in network


def test_existing_resources_are_not_reused_or_deleted(monkeypatch):
    runtime = DockerRuntime(CASE_ROOT)
    docker = Mock(return_value="old-container")
    monkeypatch.setattr(runtime, "_docker", docker)
    with pytest.raises(DockerRuntimeError, match="already has resources"):
        runtime.prepare(Match(id=1), CaseLoader().load(CASE_ROOT / "web-001"))
    assert docker.call_count == 1


def test_destroy_selects_only_owned_match_resources(monkeypatch):
    runtime = DockerRuntime()
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return f"owned-{args[0]}" if args[1] == "ls" else ""

    monkeypatch.setattr(runtime, "_docker", docker)
    runtime.destroy(Match(id=42))
    for call in calls:
        if call[1] == "ls":
            assert "label=cyberrange=true" in call and "label=match_id=42" in call
        else:
            assert call[-1] == f"owned-{call[0]}"
    assert calls[1] == ("container", "rm", "--force", "--volumes", "owned-container")


@pytest.mark.parametrize("id", [None, 0, -1, True, "1; touch /tmp/no"])
def test_invalid_match_id_never_calls_docker(monkeypatch, id):
    runtime = DockerRuntime()
    docker = Mock()
    monkeypatch.setattr(runtime, "_docker", docker)
    with pytest.raises(ValueError):
        runtime.destroy(Match(id=id))
    docker.assert_not_called()


def test_docker_failures_are_reported(monkeypatch):
    monkeypatch.setattr(subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 1, "", "denied")))
    with pytest.raises(DockerRuntimeError, match="denied"):
        DockerRuntime._docker("info")
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("docker", 1)))
    with pytest.raises(DockerRuntimeError, match="failed"):
        DockerRuntime._docker("info")


def test_missing_and_stopped_targets_cannot_start(monkeypatch):
    runtime = DockerRuntime()
    monkeypatch.setattr(runtime, "inspect", Mock(return_value=None))
    with pytest.raises(DockerRuntimeError, match="prepare"):
        runtime.start(Match(id=1))
    monkeypatch.setattr(runtime, "inspect", Mock(return_value={"State": {"Status": "exited"}}))
    with pytest.raises(DockerRuntimeError, match="only be started once"):
        runtime.start(Match(id=1))


@pytest.mark.skipif(os.getenv("RUN_DOCKER") != "1", reason="requires a real Docker daemon; set RUN_DOCKER=1")
def test_disposable_targets_and_cleanup(tmp_path):
    runtime = DockerRuntime(CASE_ROOT)
    runtime._docker("info")  # Explicit opt-in must fail, not skip, if Docker is unavailable.
    case = CaseLoader().load(CASE_ROOT / "web-001")
    engine = make_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    Base.metadata.create_all(engine)
    # Docker allocates the network names; reserve a unique suffix without touching live matches.
    first_id = secrets.randbelow(2**52) + 1
    with Session(engine) as session:
        session.add_all([Match(id=first_id), Match(id=first_id + 1)])
        session.commit()
        matches = [session.get(Match, first_id), session.get(Match, first_id + 1)]
        try:
            first = runtime.prepare(matches[0], case)
            assert first["State"]["Status"] == "created"
            first = runtime.start(matches[0])
            assert 'VERSION_ID="20.04"' in runtime._docker("exec", first["Id"], "cat", "/etc/os-release")
            config = first["HostConfig"]
            assert not config["Privileged"]
            assert config["NetworkMode"] != "host" and config["PidMode"] != "host"
            assert config["NanoCpus"] == 1_000_000_000
            assert config["Memory"] == 256 * 1024 * 1024 and config["PidsLimit"] == 128
            assert not first["Mounts"]
            network_id = next(iter(first["NetworkSettings"]["Networks"].values()))["NetworkID"]
            network = json.loads(runtime._docker("network", "inspect", network_id))[0]
            assert network["Internal"] and network["Driver"] == "bridge"
            runtime._docker("exec", first["Id"], "touch", "/checkpoint-03-marker")
            with pytest.raises(DockerRuntimeError, match="already has resources"):
                runtime.prepare(matches[0], case)
            runtime.destroy(matches[0])
            runtime.destroy(matches[0])  # Cleanup is idempotent.
            assert runtime.inspect(matches[0]) is None
            for kind in ("container", "network", "volume"):
                assert runtime._resources(kind, str(first_id)) == []
            runtime.prepare(matches[1], case)
            second = runtime.start(matches[1])
            assert second["Id"] != first["Id"]
            runtime._docker("exec", second["Id"], "test", "!", "-e", "/checkpoint-03-marker")
            with pytest.raises(DockerRuntimeError, match="only be started once"):
                runtime.start(matches[1])
        finally:
            try:
                for match in matches:
                    runtime.destroy(match)
                    for kind in ("container", "network", "volume"):
                        assert runtime._resources(kind, str(match.id)) == []
            finally:
                engine.dispose()
