import hashlib
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy import text

from portal.app.access import issue_player_token
from portal.app.cases import CaseLoader
from portal.app.database import make_engine
from portal.app.main import create_app
from portal.app.models import Match, MatchPlayer, MatchState, Player, Team
from arena_support import destroy_arena, provision_arena
from arena.app.runtime.docker import DockerRuntime, DockerRuntimeError
from shared.arena import Endpoint


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def arena_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'blue.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    engine = make_engine(url)
    with Session(engine) as session:
        players = [Player(nickname=name) for name in ("red", "blue", "outsider")]
        tokens = [issue_player_token(player) for player in players]
        session.add_all(players)
        match = Match(id=secrets.randbelow(2**52) + 1, state=MatchState.PROVISIONING)
        session.add(match)
        session.flush()
        session.add_all([
            MatchPlayer(match_id=match.id, player_id=players[0].id, team=Team.RED),
            MatchPlayer(match_id=match.id, player_id=players[1].id, team=Team.BLUE),
        ])
        session.commit()
        yield url, session, match, tokens
    engine.dispose()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_access_auth_and_cleanup(arena_db):
    url, session, match, tokens = arena_db
    match.state = MatchState.RUNNING
    match.target_host = "172.20.0.2"
    match.blue_password = secrets.token_urlsafe(32)
    password = match.blue_password
    session.commit()
    with TestClient(create_app(url)) as client:
        endpoint = f"/matches/{match.id}/access"
        assert client.get(endpoint).status_code == 401
        assert client.get(endpoint, headers=auth("wrong")).status_code == 401
        assert client.get(endpoint, headers=auth(tokens[2])).status_code == 404
        red = client.get(endpoint + "?team=BLUE&player_id=2", headers=auth(tokens[0]))
        assert red.status_code == 200 and red.json() == {"host": "172.20.0.2"}
        assert password not in red.text
        blue = client.get(endpoint, headers=auth(tokens[1]))
        assert blue.json() == {"host": "172.20.0.2", "port": 22, "username": "blue", "password": password}
        assert blue.headers["cache-control"] == "no-store"
        # DB-backed access survives an application restart.
        with TestClient(create_app(url)) as restarted:
            assert restarted.get(endpoint, headers=auth(tokens[1])).json() == blue.json()
        player = session.get(Player, 2)
        new_token = issue_player_token(player)
        session.commit()
        assert player.token_hash == hashlib.sha256(new_token.encode()).hexdigest()
        assert client.get(endpoint, headers=auth(tokens[1])).status_code == 401
        assert client.get(endpoint, headers=auth(new_token)).status_code == 200
        runtime = Mock()
        runtime.destroy.side_effect = DockerRuntimeError("daemon unavailable")
        with pytest.raises(DockerRuntimeError):
            destroy_arena(session, match, runtime)
        assert match.blue_password is None and match.target_host is None
        assert client.get(endpoint, headers=auth(new_token)).status_code == 409


@pytest.mark.parametrize("state", [state for state in MatchState if state != MatchState.RUNNING])
def test_credentials_unavailable_outside_running(arena_db, state):
    url, session, match, tokens = arena_db
    match.state = state
    match.target_host = "172.20.0.2"
    match.blue_password = "must-not-leak"
    session.commit()
    with TestClient(create_app(url)) as client:
        response = client.get(f"/matches/{match.id}/access", headers=auth(tokens[1]))
        assert response.status_code == 409
        assert "must-not-leak" not in response.text


@pytest.mark.parametrize("failure", [False, True])
def test_provisioning_uses_private_stdin_and_cleans_failure(arena_db, monkeypatch, failure):
    _, session, match, _ = arena_db
    monkeypatch.setattr("arena.app.runtime.executor._wait_for_ssh", Mock())
    runtime = Mock()
    runtime.inspect.return_value = None
    runtime.start.return_value = {"Id": "target", "NetworkSettings": {"Networks": {"net": {"IPAddress": "172.20.0.2"}}}}
    runtime.endpoints.return_value = [Endpoint(name="ssh", host="172.20.0.2", port=22)]
    if failure:
        runtime._docker.side_effect = DockerRuntimeError("chpasswd failed")
        with pytest.raises(DockerRuntimeError):
            provision_arena(session, match, CaseLoader().load(ROOT / "arena/cases/web-001"), runtime)
        assert match.state == MatchState.FAILED and match.blue_password is None
        assert match.red_key is None and match.blue_key is None
        runtime.destroy.assert_called_once()
        assert runtime.destroy.call_args.args[0].id == match.id
    else:
        provision_arena(session, match, CaseLoader().load(ROOT / "arena/cases/web-001"), runtime)
        assert match.state == MatchState.RUNNING and len(match.blue_password) >= 40
        assert runtime._docker.call_args.args == ("exec", "--interactive", "target", "chpasswd")
        assert runtime._docker.call_args.kwargs == {"stdin": f"blue:{match.blue_password}\n"}
        objective_call = runtime._docker.call_args_list[0]
        assert objective_call.kwargs == {"stdin": match.red_key}
        assert objective_call.args[-1] == "/opt/objective/red-key"
        assert match.red_key not in str(objective_call.args)
        assert match.blue_key not in str(runtime._docker.call_args_list)


def test_private_stdin_not_in_errors(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 1, "", "blue:secret"))
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(DockerRuntimeError) as error:
        DockerRuntime._docker("exec", "--interactive", "target", "chpasswd", stdin="blue:secret\n")
    assert "secret" not in str(error.value)
    assert run.call_args.kwargs["input"] == "blue:secret\n"
    assert "secret" not in str(run.call_args.args)


def test_migration_preserves_existing_players_and_matches(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "0001")
    engine = make_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO players (nickname) VALUES ('existing')"))
            connection.execute(text("INSERT INTO matches (id) VALUES (1)"))
        command.upgrade(config, "head")
        command.check(config)
        with Session(engine) as session:
            assert session.get(Player, 1).nickname == "existing"
            assert session.get(Player, 1).token_hash is None
            assert session.get(Match, 1).state == MatchState.WAITING
            assert session.get(Match, 1).blue_password is None
    finally:
        engine.dispose()


@pytest.mark.skipif(os.getenv("RUN_DOCKER") != "1", reason="requires real Docker and OpenSSH; set RUN_DOCKER=1")
def test_blue_ssh_sudo_firewall_and_revocation(arena_db, tmp_path):
    assert shutil.which("ssh"), "Install the OpenSSH client on the Docker host"
    url, session, match, tokens = arena_db
    runtime = DockerRuntime(ROOT / "arena/cases")
    case = CaseLoader().load(ROOT / "arena/cases/web-001")
    askpass = tmp_path / "askpass"
    askpass.write_text("#!/usr/bin/env python3\nimport os\nprint(os.environ['CYBERRANGE_TEST_PASSWORD'])\n")
    askpass.chmod(0o700)

    def ssh(host, password, remote_command, known_hosts="known_hosts"):
        env = os.environ | {"SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force",
                            "DISPLAY": ":0", "CYBERRANGE_TEST_PASSWORD": password}
        return subprocess.run([
            "ssh", "-F", "/dev/null", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={tmp_path / known_hosts}",
            "-o", "ConnectTimeout=3", "-o", "ConnectionAttempts=1",
            "-o", "NumberOfPasswordPrompts=1", "-o", "PreferredAuthentications=password",
            "-o", "PubkeyAuthentication=no", f"blue@{host}", remote_command,
        ], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)

    def wait_for_web(host):
        deadline = time.monotonic() + 10
        while True:
            try:
                with urlopen(f"http://{host}/", timeout=1) as response:
                    assert response.read(100) == b"Cyber Range file service\n"
                    return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

    try:
        provision_arena(session, match, case, runtime)
        with TestClient(create_app(url)) as client:
            endpoint = f"/matches/{match.id}/access"
            blue = client.get(endpoint, headers=auth(tokens[1])).json()
            red = client.get(endpoint, headers=auth(tokens[0])).json()
            assert red == {"host": blue["host"], "services": blue["services"]}
            assert "password" not in red
            from arena.app.runtime.context import RuntimeMatch
            observed = RuntimeMatch(match.id)
            assert not runtime.blue_login_seen(observed)
            result = ssh(blue["host"], blue["password"], "sudo -n id -u")
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == "0"
            assert runtime.blue_login_seen(observed)
            assert ssh(blue["host"], "wrong-password", "true").returncode != 0
            wait_for_web(blue["host"])
            result = ssh(blue["host"], blue["password"], "iptables --version && ip6tables --version")
            assert result.returncode == 0, result.stderr
            assert result.stdout.count("(nf_tables)") == 2
            result = ssh(blue["host"], blue["password"],
                         "sudo -n iptables -N CHECKPOINT4 && sudo -n iptables -X CHECKPOINT4 && "
                         "sudo -n service cyberrange-web stop")
            assert result.returncode == 0, result.stderr
            with pytest.raises(URLError):
                urlopen(f"http://{blue['host']}/", timeout=1)
            result = ssh(blue["host"], blue["password"],
                         "sudo -n service cyberrange-web start && sudo -n test -r /var/log/cyberrange-web.log")
            assert result.returncode == 0, result.stderr
            wait_for_web(blue["host"])
            old_host, old_password = blue["host"], blue["password"]
            old_red_key, old_blue_key = match.red_key, match.blue_key
            destroy_arena(session, match, runtime)
            assert client.get(endpoint, headers=auth(tokens[1])).status_code == 409
            assert ssh(old_host, old_password, "true").returncode != 0
        second = Match(id=match.id + 1, state=MatchState.PROVISIONING)
        session.add(second)
        session.commit()
        try:
            provision_arena(session, second, case, runtime)
            assert second.blue_password != old_password
            assert second.red_key != old_red_key and second.blue_key != old_blue_key
            result = ssh(second.target_host, second.blue_password, "sudo -n id -u", "second_known_hosts")
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == "0"
            # If Docker reuses the IP, the old password still cannot authenticate.
            assert ssh(second.target_host, old_password, "true", "second_known_hosts").returncode != 0
        finally:
            destroy_arena(session, second, runtime)
    finally:
        destroy_arena(session, match, runtime)
