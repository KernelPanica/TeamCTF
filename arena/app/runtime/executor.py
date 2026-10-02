"""Only fixed game operations; no client-supplied executable commands."""
import secrets
import socket
import time
from pathlib import Path

from shared.arena import Endpoint, Observation
from shared.cases import CaseLoader
from arena.app.checks.health import HealthChecker
from arena.app.checks.exploit import ExploitChecker
from arena.app.checks.red_zone import RedZoneChecker
from .context import RuntimeMatch
from .docker import DockerRuntime, DockerRuntimeError


def _wait_for_ssh(host):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, 22), timeout=1) as connection:
                connection.settimeout(1)
                if connection.recv(255).startswith(b"SSH-2.0-"):
                    return
        except OSError:
            pass
        time.sleep(0.1)
    raise DockerRuntimeError("SSH did not become ready")


class Executor:
    def __init__(self, cases_directory=Path("arena/cases"), runtime=None):
        self.cases_directory = Path(cases_directory)
        self.runtime = runtime if runtime is not None else DockerRuntime(self.cases_directory)

    def create(self, request):
        case = CaseLoader().load(self.cases_directory / request.case_id)
        match = RuntimeMatch(request.match_id, case.id)
        # Never take ownership of someone else's existing resources.
        self.runtime.prepare(match, case)
        try:
            target = self.runtime.start(match)
            self.runtime._docker(
                "exec", "--interactive", target["Id"], "python3", "-c",
                "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                "p.parent.mkdir(parents=True, exist_ok=True); "
                "p.write_bytes(sys.stdin.buffer.read()); p.chmod(0o444)",
                case.red.objective_path, stdin=request.red_key.get_secret_value(),
            )
            password = secrets.token_urlsafe(32)
            self.runtime._docker("exec", "--interactive", target["Id"], "chpasswd", stdin=f"blue:{password}\n")
            match.target_host = next(iter(target["NetworkSettings"]["Networks"].values()))["IPAddress"]
            _wait_for_ssh(match.target_host)
            endpoints = self.runtime.endpoints(target, case, match.target_host)
            return match, case, password, endpoints
        except Exception:
            self.runtime.destroy(match)
            raise

    def observe(self, match, case, red_key):
        began = time.monotonic()
        health = HealthChecker(self.cases_directory).check(match, case)
        exploit = ExploitChecker(self.cases_directory).check(match, case, red_key)
        red = RedZoneChecker(self.runtime, self.cases_directory).check(match, case, red_key)
        return Observation(health=health, exploit=exploit, surface=red["surface"],
                           red_exploit=red["exploit"], red_services=red["services"],
                           duration_seconds=time.monotonic() - began)

    def destroy(self, match_id):
        self.runtime.destroy(RuntimeMatch(match_id))

    def cleanup(self):
        self.runtime.cleanup_owned()
