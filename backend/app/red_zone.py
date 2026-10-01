"""Functional and exploit probes from the same network zone as RED."""
import secrets
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from .cases import CaseSpec
from .checks import checked_case_directory, target_url
from .health import HealthChecker
from .models import Match, MatchState
from .runtime import DockerRuntime, DockerRuntimeError


class RedZoneChecker:
    def __init__(self, runtime: DockerRuntime, cases_directory: Path = Path("cases")):
        self.runtime = runtime
        self.cases_directory = Path(cases_directory).resolve()

    def _probe_image(self, match: Match, case: CaseSpec, target: dict) -> str:
        directory = checked_case_directory(match, case, self.cases_directory)
        image_id = target["Image"]
        tag = f"range-case-{case.id}-probe:{image_id.removeprefix('sha256:')[:12]}"
        dockerfile = """ARG BASE\nFROM ${BASE}\nCOPY checks/ /opt/checks/\nRUN rm -rf /opt/objective\nENTRYPOINT []\nCMD [\"sleep\", \"60\"]\n"""
        with tempfile.TemporaryDirectory(prefix="cyberrange-probe-") as temporary:
            path = Path(temporary) / "Dockerfile"
            path.write_text(dockerfile)
            self.runtime._docker(
                "build", "--force-rm", "--tag", tag, "--build-arg", f"BASE={image_id}",
                "--file", str(path), str(directory), timeout=600,
            )
        return tag

    def _exit_code(self, probe: str, script: str, url: str, *args: str,
                   stdin: str | None = None) -> int:
        output = self.runtime._docker(
            "exec", "--interactive", probe, "sh", "-c",
            'python3 "$@"; code=$?; printf "%s" "$code"', "probe",
            f"/opt/checks/{script}", url, *args, stdin=stdin,
        )
        try:
            return int(output)
        except ValueError as exc:
            raise DockerRuntimeError("probe returned an invalid exit code") from exc

    def check(self, match: Match, case: CaseSpec, expected_key: str) -> dict:
        if not expected_key or len(expected_key.encode()) > 4096:
            raise ValueError("expected objective must contain 1–4096 bytes")
        checked_case_directory(match, case, self.cases_directory)
        target = self.runtime.inspect(match)
        if target is None or not target["State"]["Running"]:
            raise DockerRuntimeError("running target is missing")
        networks = target["NetworkSettings"]["Networks"]
        if len(networks) != 1:
            raise DockerRuntimeError("target must have exactly one match network")
        network_id = next(iter(networks.values()))["NetworkID"]
        target_name = target["Name"].removeprefix("/")
        probe_name = f"range-match-{match.id}-probe-{secrets.token_hex(4)}"
        labels = ["--label", "cyberrange=true", "--label", f"match_id={match.id}",
                  "--label", "role=red-probe"]
        probe = None
        try:
            probe = self.runtime._docker(
                "create", "--name", probe_name, "--network", network_id, *labels,
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--memory", "64m", "--memory-swap", "64m", "--pids-limit", "32",
                self._probe_image(match, case, target),
            )
            self.runtime._docker("start", probe)
            self.runtime._docker("exec", probe, "test", "!", "-e", "/opt/objective")
            services = {}
            for service in case.required_services:
                code = self._exit_code(
                    probe, Path(case.checks.health).name,
                    target_url(target_name, service.port), service.name, service.protocol,
                )
                services[service.name] = "AVAILABLE" if code == 0 else "UNAVAILABLE"
            exploit_code = self._exit_code(
                probe, Path(case.checks.exploit).name,
                target_url(target_name, case.required_services[0].port), stdin=expected_key,
            )
            exploit = {0: "VULNERABLE", 10: "PATCHED", 11: "UNREACHABLE"}.get(
                exploit_code, "ERROR"
            )
            return {
                "surface": "AVAILABLE" if all(v == "AVAILABLE" for v in services.values()) else "UNAVAILABLE",
                "services": services,
                "exploit": exploit,
            }
        finally:
            if probe:
                self.runtime._docker("container", "rm", "--force", "--volumes", probe)


def check_red_defense(session: Session, match: Match, case: CaseSpec, expected_key: str,
                      runtime: DockerRuntime, cases_directory: Path = Path("cases")) -> dict:
    health = HealthChecker(cases_directory).poll(session, match, case)
    red = RedZoneChecker(runtime, cases_directory).check(match, case, expected_key)
    session.refresh(match)
    eligible = (
        match.state == MatchState.RUNNING
        and bool(match.target_host)
        and health["status"] == "HEALTHY"
        and red["surface"] == "AVAILABLE"
        and red["exploit"] == "PATCHED"
    )
    return {"health": health, "red_zone": red, "blue_eligible": eligible}
