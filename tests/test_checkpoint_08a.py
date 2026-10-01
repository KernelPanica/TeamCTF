import asyncio
import os
import secrets
import subprocess
import sys
import threading
import socket
import time
from urllib.request import urlopen
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.orm import Session

from arena.app.main import create_app as create_agent
from arena.app.provider import LocalArenaProvider
from arena.app.service import ArenaService
from backend.app.arena_provider import RemoteArenaProvider
from backend.app.database import Base, make_engine
from backend.app.main import create_app as create_portal
from backend.app.models import Match, MatchState, MatchEvent
from backend.app.provisioning import provision_arena, destroy_arena
from backend.app.observations import collect_observations
from shared.arena import ArenaError, CreateMatch, Endpoint, Observation
from shared.cases import CaseLoader


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-arena-token-" + "x" * 32


class FakeExecutor:
    def __init__(self):
        self.created = []
        self.destroyed = []
        self.cleaned = 0
        self.fail_destroy = False
        self.block = threading.Event()
        self.block.set()

    def cleanup(self):
        self.cleaned += 1

    def create(self, request):
        self.created.append(request.match_id)
        assert self.block.wait(5)
        return SimpleNamespace(id=request.match_id), CaseLoader().load(ROOT / "cases/web-001"), "blue-password", [
            Endpoint(name="ssh", host="192.0.2.1", port=22001),
            Endpoint(name="web", host="192.0.2.1", port=18001),
        ]

    def observe(self, match, case, key):
        return Observation(health={"web": {"status": "HEALTHY", "reason": "OK"}},
                           exploit="PATCHED", surface="AVAILABLE", red_exploit="PATCHED",
                           red_services={"web": "AVAILABLE"}, duration_seconds=0.1)

    def destroy(self, match_id):
        if self.fail_destroy:
            raise RuntimeError("secret Docker details")
        self.destroyed.append(match_id)


async def wait_ready(provider, match_id):
    for _ in range(500):
        result = await provider.get_match(match_id)
        if result.state != "PROVISIONING":
            assert result.state == "READY", result
            return result
        await asyncio.sleep(0.01)
    pytest.fail("provisioning did not finish")


async def wait_events(provider, match_id, after=0):
    for _ in range(500):
        page = await provider.get_events(match_id, after)
        if page.events:
            return page
        await asyncio.sleep(0.01)
    pytest.fail("no observations")


@pytest.mark.parametrize("remote", [False, True])
def test_provider_contract_lifecycle_and_portal_persistence(tmp_path, remote):
    async def scenario():
        executor = FakeExecutor()
        service = ArenaService(executor, interval=0.02)
        app = create_agent(service, TOKEN)
        async with app.router.lifespan_context(app):
            provider = RemoteArenaProvider("https://arena.test", TOKEN, transport=httpx.ASGITransport(app=app)) if remote else LocalArenaProvider(service)
            try:
                request = CreateMatch(match_id=1, run_id=uuid4(), case_id="web-001", red_key="private-objective")
                first, repeated = await asyncio.gather(provider.create_match(request), provider.create_match(request))
                assert first.run_id == repeated.run_id
                ready = await wait_ready(provider, 1)
                assert ready.blue_password.get_secret_value() == "blue-password"
                assert [(e.name, e.port) for e in ready.endpoints] == [("ssh", 22001), ("web", 18001)]
                assert executor.created == [1]
                with pytest.raises(ArenaError) as error:
                    await provider.create_match(request.model_copy(update={"run_id": uuid4()}))
                assert error.value.status == 409
                page = await wait_events(provider, 1)
                assert page.cursor == page.events[-1].sequence
                assert "private-objective" not in page.model_dump_json()
                assert "blue-password" not in page.model_dump_json()
                with pytest.raises(ArenaError):
                    await provider.get_events(1, 99999)
                executor.fail_destroy = True
                with pytest.raises(ArenaError):
                    await provider.destroy_match(1)
                failed = await provider.get_match(1)
                assert failed.blue_password is None and failed.error == "CLEANUP_FAILED"
                executor.fail_destroy = False
                await provider.destroy_match(1)
                await provider.destroy_match(1)
                assert (await provider.get_match(1)).state == "DELETED"
                assert (await provider.create_match(request)).state == "DELETED"
                assert not service.matches[1].events and service.matches[1].task is None

                db = make_engine(f"sqlite:///{tmp_path / 'portal.db'}")
                Base.metadata.create_all(db)
                with Session(db) as session:
                    match = Match(id=2, state=MatchState.PROVISIONING)
                    session.add(match)
                    session.commit()
                    await provision_arena(session, match, CaseLoader().load(ROOT / "cases/web-001"), provider)
                    assert match.state == MatchState.RUNNING
                    assert match.blue_key and match.red_key and match.blue_password == "blue-password"
                    await wait_events(provider, 2)
                    result = await collect_observations(session, match, provider)
                    assert result["blue_eligible"] is True
                    cursor = match.arena_cursor
                    assert cursor > 0
                    assert session.query(MatchEvent).filter_by(type="ARENA_OBSERVATION").count() == cursor
                    await collect_observations(session, match, provider)
                    assert session.query(MatchEvent).filter_by(type="ARENA_OBSERVATION").count() == match.arena_cursor
                    await destroy_arena(session, match, provider)
                    assert match.blue_key is None and match.red_key is None and match.blue_password is None
                    assert not match.arena_cleanup_pending
                db.dispose()
            finally:
                if remote:
                    await provider.close()
        assert executor.cleaned == 1
    asyncio.run(scenario())


def test_authentication_validation_and_no_command_api():
    async def scenario():
        service = ArenaService(FakeExecutor())
        app = create_agent(service, TOKEN)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://arena.test") as client:
                for method, path in [("POST", "/matches"), ("GET", "/matches/1"),
                                     ("GET", "/matches/1/events"), ("DELETE", "/matches/1")]:
                    assert (await client.request(method, path, json={})).status_code == 401
                    assert (await client.request(method, path, headers={"Authorization": "Bearer wrong"}, json={})).status_code == 401
                client.headers["Authorization"] = "Bearer " + TOKEN
                for path in ("/exec", "/shell", "/command"):
                    assert (await client.post(path, json={"command": "id"})).status_code == 404
                response = await client.post("/matches", json={"match_id": 1, "run_id": str(uuid4()),
                    "case_id": "../../secret", "red_key": "secret-objective", "command": "id"})
                assert response.status_code == 422 and "secret-objective" not in response.text
                assert response.headers["cache-control"] == "no-store"
                assert service.matches == {}
    asyncio.run(scenario())


def test_delete_during_creation_and_restart():
    async def scenario():
        executor = FakeExecutor()
        executor.block.clear()
        service = ArenaService(executor)
        await service.start()
        request = CreateMatch(match_id=1, run_id=uuid4(), case_id="web-001", red_key="key")
        await service.create_match(request)
        deletion = asyncio.create_task(service.destroy_match(1))
        await asyncio.sleep(0.02)
        assert not deletion.done()
        executor.block.set()
        await deletion
        assert (await service.get_match(1)).state == "DELETED"
        assert executor.destroyed == [1]
        restarted = ArenaService(executor)
        await restarted.start()
        assert restarted.instance_id != service.instance_id and executor.cleaned == 2
        with pytest.raises(ArenaError) as error:
            await restarted.get_match(1)
        assert error.value.status == 404
        await service.close()
        await restarted.close()
    asyncio.run(scenario())


def test_portal_import_and_start_without_runtime_or_docker(tmp_path):
    # A clean interpreter catches indirect imports hidden by other tests.
    code = """
import asyncio, importlib.abc, sys
class NoArena(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname == 'arena' or fullname.startswith('arena.'):
            raise AssertionError('Portal imported Arena runtime')
sys.meta_path.insert(0, NoArena())
from backend.app.main import create_app
from backend.app.database import Base, make_engine
from fastapi.testclient import TestClient
engine = make_engine()
Base.metadata.create_all(engine)
with TestClient(create_app(arena_provider=object())) as client:
    assert client.get('/health').status_code == 200
    assert client.get('/').status_code == 200
with TestClient(create_app()) as client:
    assert client.get('/health').status_code == 200
"""
    env = os.environ | {"DATABASE_URL": f"sqlite:///{tmp_path / 'no-docker.db'}", "PATH": str(tmp_path), "ARENA_PROVIDER": "remote"}
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    for path in (ROOT / "arena").rglob("*.py"):
        assert "backend.app" not in path.read_text()
    dockerfile = (ROOT / "arena/Dockerfile").read_text()
    assert "COPY . ." not in dockerfile and "COPY backend" not in dockerfile


def test_https_required_and_transport_failures():
    with pytest.raises(ValueError):
        RemoteArenaProvider("http://arena", TOKEN)
    async def scenario():
        async def broken(request):
            raise httpx.ConnectError("private internal details")
        provider = RemoteArenaProvider("https://arena", TOKEN, transport=httpx.MockTransport(broken))
        try:
            with pytest.raises(ArenaError) as error:
                await provider.get_match(1)
            assert "private" not in str(error.value)
        finally:
            await provider.close()
    asyncio.run(scenario())


def test_cursor_gap_is_explicit_in_both_providers():
    async def scenario():
        service = ArenaService(FakeExecutor(), interval=0.001)
        app = create_agent(service, TOKEN)
        async with app.router.lifespan_context(app):
            local = LocalArenaProvider(service)
            remote = RemoteArenaProvider("https://arena.test", TOKEN, transport=httpx.ASGITransport(app=app))
            try:
                await local.create_match(CreateMatch(match_id=1, run_id=uuid4(), case_id="web-001", red_key="key"))
                await wait_events(local, 1)
                entry = service.matches[1]
                last = entry.events[-1].model_copy(update={"sequence": 1002})
                entry.events.clear()
                entry.events.append(last)
                entry.sequence = 1002
                for provider in (local, remote):
                    with pytest.raises(ArenaError, match="EVENT_GAP"):
                        await provider.get_events(1, 0)
            finally:
                await remote.close()
    asyncio.run(scenario())


def test_lost_arena_revokes_access_and_retries_cleanup(tmp_path):
    from backend.app.arena_reconcile import reconcile_once
    from unittest.mock import AsyncMock
    async def scenario():
        db = make_engine(f"sqlite:///{tmp_path / 'lost.db'}")
        Base.metadata.create_all(db)
        with Session(db) as session:
            match = Match(id=1, state=MatchState.RUNNING, arena_run_id=str(uuid4()), arena_instance_id=str(uuid4()),
                target_host="192.0.2.1", blue_password="secret", red_key="red", blue_key="blue", arena_cleanup_pending=True)
            session.add(match)
            session.commit()
            provider = SimpleNamespace(get_match=AsyncMock(side_effect=ArenaError("NOT_FOUND", 404)),
                                       destroy_match=AsyncMock(side_effect=ArenaError("UNAVAILABLE")))
            await reconcile_once(db, provider)
            session.refresh(match)
            assert match.state == MatchState.FAILED and match.blue_password is None
            assert match.red_key is None and match.blue_key is None and match.arena_cleanup_pending
            provider.destroy_match.side_effect = None
            await reconcile_once(db, provider)
            session.refresh(match)
            assert not match.arena_cleanup_pending
            assert provider.destroy_match.await_count == 2
            # A requested delete is retried even if game state has not finalized yet.
            match.state = MatchState.RUNNING
            match.arena_cleanup_pending = True
            session.commit()
            provider.get_match.side_effect = ArenaError("UNAVAILABLE")
            # A transport outage must not prevent retrying the separately requested delete.
            await reconcile_once(db, provider)
            session.refresh(match)
            assert not match.arena_cleanup_pending
        db.dispose()
    asyncio.run(scenario())


def test_firewall_verification_fails_closed(monkeypatch):
    from arena.app.runtime import firewall
    from arena.app.runtime.docker import DockerRuntime
    from arena.app.runtime.context import RuntimeMatch
    from unittest.mock import Mock
    commands = []
    def output(*args):
        commands.append(args)
        name = args[1]
        if name in firewall.PARENTS:
            return f"-N {name}\n-A {name} -i crarena+ -j {firewall.PARENTS[name]}\n"
        return "\n".join(f"-A {name} " + " ".join(rule) for rule in firewall.RULES[name])
    monkeypatch.setattr(firewall, "command", output)
    firewall.verify()
    monkeypatch.setattr(firewall, "command", lambda *args: "-A INPUT -j ACCEPT")
    with pytest.raises(RuntimeError):
        firewall.verify()
    runtime = DockerRuntime(ROOT / "cases", publish_ip="127.0.0.1")
    docker = Mock()
    monkeypatch.setattr(runtime, "_docker", docker)
    # Verify itself is exercised before launching any target; build is harmless.
    monkeypatch.setattr(runtime, "_resources", lambda *args: [])
    def build(*args, **kwargs):
        if args[0] == "build":
            Path(args[args.index("--iidfile") + 1]).write_text("sha256:test")
            return ""
        if args[:2] == ("image", "inspect"):
            return '[{"Config": {}}]'
        pytest.fail("runtime attempted to create an unprotected arena")
    monkeypatch.setattr(runtime, "_docker", build)
    with pytest.raises(RuntimeError):
        runtime.prepare(RuntimeMatch(1), CaseLoader().load(ROOT / "cases/web-001"))


@pytest.mark.skipif(os.getenv("RUN_ARENA") != "1", reason="requires root Docker host and installed Arena firewall")
def test_remote_https_real_target_checks_isolation_and_cleanup(tmp_path):
    from arena.app.runtime.docker import DockerRuntime
    from arena.app.runtime.context import RuntimeMatch
    from arena.app.runtime.firewall import verify
    verify()
    # Route lookup selects a host address without sending application traffic.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect(("192.0.2.1", 9))
        management_ip = os.getenv("ARENA_TEST_MANAGEMENT_IP", route.getsockname()[0])
    with socket.socket() as listener:
        listener.bind((management_ip, 0))
        port = listener.getsockname()[1]
    cert, key = tmp_path / "tls.crt", tmp_path / "tls.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=arena-test", "-addext", f"subjectAltName=IP:{management_ip}",
        "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    owner = "test-" + secrets.token_hex(8)
    token = secrets.token_urlsafe(32)
    env = os.environ | {"ARENA_OWNER": owner, "ARENA_TOKEN": token,
        "ARENA_MANAGEMENT_IP": management_ip, "ARENA_MANAGEMENT_PORT": str(port),
        "ARENA_GAME_IP": "127.0.0.1", "ARENA_PUBLIC_HOST": "127.0.0.1",
        "ARENA_TLS_CERT": str(cert), "ARENA_TLS_KEY": str(key), "ARENA_CASES": str(ROOT / "cases")}
    log = (tmp_path / "agent.log").open("w+")
    process = subprocess.Popen([sys.executable, "-m", "arena.app.serve"], cwd=ROOT, env=env, stdout=log, stderr=log)
    runtime = DockerRuntime(ROOT / "cases", owner=owner)
    match_id = secrets.randbelow(2**50) + 1
    request = CreateMatch(match_id=match_id, run_id=uuid4(), case_id="web-001", red_key="RED_" + secrets.token_urlsafe(32))

    async def scenario():
        provider = RemoteArenaProvider(f"https://{management_ip}:{port}", token, ca_file=str(cert))
        try:
            deadline = time.monotonic() + 20
            while True:
                assert process.poll() is None, "Agent exited; see agent.log"
                try:
                    await provider.get_match(match_id)
                except ArenaError as error:
                    if error.status == 404:
                        break
                    assert time.monotonic() < deadline, "Agent did not start"
                    await asyncio.sleep(0.1)
            untrusted = RemoteArenaProvider(f"https://{management_ip}:{port}", token)
            try:
                with pytest.raises(ArenaError):
                    await untrusted.get_match(match_id)
            finally:
                await untrusted.close()
            await provider.create_match(request)
            deadline = time.monotonic() + 900
            while True:
                status = await provider.get_match(match_id)
                if status.state != "PROVISIONING":
                    assert status.state == "READY", status
                    break
                assert time.monotonic() < deadline
                await asyncio.sleep(0.2)
            assert (await provider.create_match(request)).run_id == request.run_id
            context = RuntimeMatch(match_id)
            target = runtime.inspect(context)
            assert "20.04" in runtime._docker("exec", target["Id"], "cat", "/etc/os-release")
            web = next(e for e in status.endpoints if e.name == "web")
            ssh = next(e for e in status.endpoints if e.name == "ssh")
            askpass = tmp_path / "askpass"
            askpass.write_text("#!/usr/bin/env python3\nimport os\nprint(os.environ['ARENA_TEST_PASSWORD'])\n")
            askpass.chmod(0o700)
            ssh_result = subprocess.run(["ssh", "-F", "/dev/null", "-p", str(ssh.port),
                "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={tmp_path / 'known_hosts'}",
                "-o", "ConnectTimeout=5", "-o", "NumberOfPasswordPrompts=1",
                "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
                f"blue@{ssh.host}", "sudo -n id -u"], stdin=subprocess.DEVNULL,
                env=os.environ | {"SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0",
                                  "ARENA_TEST_PASSWORD": status.blue_password.get_secret_value()},
                capture_output=True, text=True, timeout=30)
            assert ssh_result.returncode == 0 and ssh_result.stdout.strip() == "0", ssh_result.stderr
            deadline = time.monotonic() + 15
            while True:
                try:
                    with urlopen(f"http://{web.host}:{web.port}/", timeout=2) as response:
                        assert response.read() == b"Cyber Range file service\n"
                    break
                except OSError:
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.1)
            cursor = 0

            async def observed(exploit, surface="AVAILABLE"):
                nonlocal cursor
                deadline = time.monotonic() + 120
                while True:
                    page = await provider.get_events(match_id, cursor)
                    cursor = page.cursor
                    for event in page.events:
                        observation = event.observation
                        if observation.red_exploit == exploit and observation.surface == surface:
                            return observation
                    assert time.monotonic() < deadline, "desired observation not received"
                    await asyncio.sleep(0.2)

            initial = await observed("VULNERABLE")
            assert initial.health["web"].status == "HEALTHY"
            # Network denial, independently of API auth: try TCP before HTTP.
            gateway = next(iter(target["NetworkSettings"]["Networks"].values()))["Gateway"]
            script = """import socket,sys
for host in sys.argv[1:-1]:
    try:
        connection=socket.create_connection((host,int(sys.argv[-1])),timeout=2)
    except OSError:
        continue
    connection.close()
    raise SystemExit('management reachable from target: '+host)
"""
            runtime._docker("exec", target["Id"], "python3", "-c", script, management_ip, gateway, str(port))
            # Deliberate root/NET_ADMIN attacker still cannot change host policy.
            runtime._docker("exec", target["Id"], "iptables", "-F")
            runtime._docker("exec", target["Id"], "python3", "-c", script, management_ip, gateway, str(port))
            runtime._docker("exec", target["Id"], "service", "cyberrange-web", "stop")
            stopped = await observed("UNREACHABLE", "UNAVAILABLE")
            assert stopped.health["web"].status == "UNHEALTHY"
            runtime._docker("exec", target["Id"], "python3", "-c",
                "from pathlib import Path; p=Path('/opt/service/server.py'); s=p.read_text(); "
                "assert 'elif url.path == \"/download\":' in s; p.write_text(s.replace('elif url.path == \"/download\":','elif False:'))")
            runtime._docker("exec", target["Id"], "service", "cyberrange-web", "start")
            patched = await observed("PATCHED")
            assert patched.health["web"].status == "HEALTHY"
            await provider.destroy_match(match_id)
            await provider.destroy_match(match_id)
            assert (await provider.get_match(match_id)).blue_password is None
            for kind in ("container", "network", "volume"):
                assert runtime._resources(kind, str(match_id)) == []
            # Abrupt Agent restart must clean a live arena before accepting requests.
            second = request.model_copy(update={"match_id": match_id + 1, "run_id": uuid4()})
            await provider.create_match(second)
            deadline = time.monotonic() + 120
            while (await provider.get_match(second.match_id)).state == "PROVISIONING":
                assert time.monotonic() < deadline
                await asyncio.sleep(0.2)
            assert runtime.inspect(RuntimeMatch(second.match_id)) is not None
        finally:
            await provider.close()

    try:
        asyncio.run(scenario())
        process.kill()
        process.wait(timeout=10)
        process = subprocess.Popen([sys.executable, "-m", "arena.app.serve"], cwd=ROOT, env=env, stdout=log, stderr=log)
        deadline = time.monotonic() + 30
        while any(runtime._resources(kind, str(match_id + 1)) for kind in ("container", "network", "volume")):
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.2)
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        runtime.cleanup_owned()
        log.close()
