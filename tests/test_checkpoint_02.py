import importlib.util
import random
import shutil
import subprocess
import sys
from http.server import HTTPServer
from pathlib import Path
from threading import Thread

import pytest
import yaml

from portal.app.cases import CaseCatalog, CaseFormatError, CaseLoader


EXAMPLE = Path(__file__).resolve().parents[1] / "arena/cases" / "web-001"


@pytest.fixture
def case_dir(tmp_path):
    path = tmp_path / "web-001"
    shutil.copytree(EXAMPLE, path)
    return path


def test_example_and_cli():
    case = CaseLoader().load(EXAMPLE)
    assert case.id == "web-001"
    assert case.required_services[0].port == 80
    result = subprocess.run([sys.executable, "-m", "portal.app.cli", "cases", "validate"],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "web-001: READY" in result.stdout


@pytest.mark.parametrize("mutation", [
    lambda data: data.update(id="wrong-id"),
    lambda data: data.update(name=" "),
    lambda data: data.update(required_services=[]),
    lambda data: data["required_services"][0].update(port=65536),
    lambda data: data["required_services"][0].update(port=True),
    lambda data: data["required_services"][0].update(protocol="http"),
    lambda data: data["required_services"].append(data["required_services"][0].copy()),
    lambda data: data["red"].update(objective_path="relative/key"),
    lambda data: data["blue"].update(stabilization_seconds=0),
    lambda data: data["checks"].update(health="../outside.py"),
    lambda data: data["checks"].update(health="/tmp/outside.py"),
    lambda data: data["checks"].update(exploit="checks/missing.py"),
    lambda data: data.pop("red"),
])
def test_invalid_spec_excluded(case_dir, mutation):
    manifest = case_dir / "case.yaml"
    data = yaml.safe_load(manifest.read_text())
    mutation(data)
    manifest.write_text(yaml.safe_dump(data))
    catalog = CaseCatalog(case_dir.parent)
    assert catalog.list() == []
    assert "web-001" in catalog.invalid
    with pytest.raises(KeyError):
        catalog.get("web-001")
    with pytest.raises(CaseFormatError, match="no READY"):
        catalog.random(1)


@pytest.mark.parametrize("content", ["id: [broken", "- not a mapping", "!!python/object:os.system {}", ""])
def test_invalid_yaml_cli(case_dir, content):
    (case_dir / "case.yaml").write_text(content)
    result = subprocess.run([sys.executable, "-m", "portal.app.cli", "cases", "validate",
                             "--directory", str(case_dir.parent)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "web-001: INVALID" in result.stdout


@pytest.mark.parametrize("file,content", [
    ("Dockerfile", "FROM ubuntu:22.04\n"),
    ("Dockerfile", "FROM ubuntu:20.04\nFROM alpine\n"),
    ("Dockerfile", "# FROM ubuntu:20.04\n"),
    ("checks/health.py", "this is invalid python !"),
])
def test_invalid_files(case_dir, file, content):
    (case_dir / file).write_text(content)
    with pytest.raises(CaseFormatError):
        CaseLoader().load(case_dir)


def test_missing_rootfs_and_symlink_escape(case_dir, tmp_path):
    shutil.rmtree(case_dir / "rootfs")
    with pytest.raises(CaseFormatError, match="rootfs"):
        CaseLoader().load(case_dir)
    (case_dir / "rootfs").mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("pass")
    checker = case_dir / "checks/health.py"
    checker.unlink()
    checker.symlink_to(outside)
    with pytest.raises(CaseFormatError, match="escapes"):
        CaseLoader().load(case_dir)


def test_catalog_seed_and_mixed_cases(case_dir):
    root = case_dir.parent
    for id in ("web-003", "web-002"):
        copy = root / id
        shutil.copytree(case_dir, copy)
        manifest = copy / "case.yaml"
        manifest.write_text(manifest.read_text().replace("web-001", id))
    (root / "broken").mkdir()
    catalog = CaseCatalog(root)
    assert [case.id for case in catalog.list()] == ["web-001", "web-002", "web-003"]
    assert set(catalog.invalid) == {"broken"}
    assert catalog.get("web-002").id == "web-002"
    state = random.getstate()
    choices = [catalog.random(seed).id for seed in range(20)]
    assert choices == [CaseCatalog(root).random(seed).id for seed in range(20)]
    assert len(set(choices)) > 1
    assert random.getstate() == state


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_service_and_checkers(tmp_path, monkeypatch):
    server_module = load_module("case_server", EXAMPLE / "rootfs/server.py")
    health = load_module("case_health", EXAMPLE / "checks/health.py")
    exploit = load_module("case_exploit", EXAMPLE / "checks/exploit.py")
    objective = tmp_path / "key"
    objective.write_text("test-key")
    # Map the target-only objective to a temporary file for the local contract test.
    monkeypatch.setattr(server_module, "Path", lambda path: objective if path == "/opt/objective/red-key" else Path(path))
    server = HTTPServer(("127.0.0.1", 0), server_module.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        assert health.check(url)
        assert exploit.check(url, "test-key")
        assert not exploit.check(url, "wrong-key")
        objective.unlink()
        assert not exploit.check(url, "test-key")
        assert health.check(url)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert not health.check(url)
    assert not exploit.check(url, "test-key")
