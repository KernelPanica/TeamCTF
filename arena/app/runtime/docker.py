"""Disposable targets controlled by the host Docker CLI."""
import json
import subprocess
import tempfile
import hashlib
from pathlib import Path

from shared.cases import CaseLoader, CaseSpec
from arena.app.runtime.context import RuntimeMatch as Match
from shared.arena import Endpoint


class DockerRuntimeError(RuntimeError):
    pass


class DockerRuntime:
    def __init__(self, cases_directory: Path = Path("arena/cases"), *, owner="local", publish_ip=None, public_host=None,
                 game_port_min=30000, game_port_max=39999, reserved_ports=(), management_port=8443):
        self.cases_directory = Path(cases_directory).resolve()
        self.owner = owner
        self.publish_ip = publish_ip
        self.public_host = public_host or publish_ip
        self.port_pool = None
        if publish_ip:
            from arena.app.networking.ports import PortPool
            self.port_pool = PortPool(publish_ip, game_port_min, game_port_max,
                                      {*reserved_ports, management_port})

    @staticmethod
    def _match_id(match: Match) -> str:
        if type(match.id) is not int or match.id <= 0:
            raise ValueError("match must have a positive persisted integer id")
        return str(match.id)

    @staticmethod
    def _docker(*args: str, timeout: int = 60, stdin: str | None = None) -> str:
        try:
            result = subprocess.run(
                ["docker", *args], capture_output=True, text=True, timeout=timeout, input=stdin,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerRuntimeError(f"docker {args[0]} failed: {exc}") from exc
        if result.returncode:
            detail = "command with private stdin failed" if stdin is not None else result.stderr.strip()
            raise DockerRuntimeError(f"docker {args[0]} failed: {detail}")
        return result.stdout.strip()

    def _resources(self, kind: str, match_id: str, role=None) -> list[str]:
        args = [kind, "ls", "--quiet"]
        if kind == "container":
            args.append("--all")
        if role:
            args += ["--filter", f"label=role={role}"]
        return self._docker(
            *args, "--filter", "label=cyberrange=true",
            "--filter", f"label=match_id={match_id}",
            "--filter", f"label=arena_owner={self.owner}",
        ).split()

    def prepare(self, match: Match, case: CaseSpec) -> dict:
        match_id = self._match_id(match)
        directory = self.cases_directory / case.id
        if directory.is_symlink() or not directory.resolve().is_relative_to(self.cases_directory):
            raise ValueError("case directory must be inside the configured cases directory")
        if CaseLoader().load(directory) != case:
            raise ValueError("case specification changed; reload the catalog")
        if any(self._resources(kind, match_id) for kind in ("container", "network", "volume")):
            raise DockerRuntimeError("match already has resources; destroy them before preparing again")

        # Build once per prepare; Docker's layer cache reuses the immutable case image.
        with tempfile.TemporaryDirectory(prefix="cyberrange-build-") as temporary:
            image_file = Path(temporary) / "image-id"
            self._docker(
                "build", "--force-rm", "--tag", f"range-case-{case.id}:latest",
                "--iidfile", str(image_file), str(directory), timeout=600,
            )
            image_id = image_file.read_text().strip()
        image = json.loads(self._docker("image", "inspect", image_id))[0]
        if image["Config"].get("Volumes"):
            raise DockerRuntimeError("case images must not declare volumes")

        labels = ["--label", "cyberrange=true", "--label", f"match_id={match_id}",
                  "--label", f"arena_owner={self.owner}"]
        network_options = ["--internal"]
        published = []
        if self.publish_ip:
            from .firewall import verify
            verify(require_management=True)
            bridge = "crarena" + hashlib.sha256(f"{self.owner}:{match_id}".encode()).hexdigest()[:8]
            network_options = ["--opt", f"com.docker.network.bridge.name={bridge}"]
            ports = {(22, "tcp")} | {(s.port, s.protocol) for s in case.required_services}
            mappings = self.port_pool.allocate(match_id, ports)
            for (port, protocol), host_port in mappings.items():
                published += ["--publish", f"{self.publish_ip}:{host_port}:{port}/{protocol}"]
        network_id = container_id = None
        try:
            network_id = self._docker(
                "network", "create", "--driver", "bridge", *network_options,
                *labels, f"range-match-{match_id}-net",
            )
            container_id = self._docker(
                "create", "--name", f"range-match-{match_id}-target",
                "--network", network_id, *labels, "--label", "role=target", *published,
                "--cpus", "1", "--memory", "256m", "--memory-swap", "256m",
                "--pids-limit", "128", "--init", "--restart", "no",
                "--cap-add", "NET_ADMIN",
                "--log-driver", "json-file", "--log-opt", "max-size=10m",
                "--log-opt", "max-file=2", image_id,
            )
            target = self.inspect(match)
            if target is None:
                raise DockerRuntimeError("created target is missing")
            return target
        except Exception as exc:
            # Only roll back IDs created by this call, never resources from a name collision.
            cleaned = True
            for kind, resource_id in (("container", container_id), ("network", network_id)):
                if resource_id:
                    try:
                        if kind == "container":
                            self._docker("container", "rm", "--force", "--volumes", resource_id)
                        else:
                            self._docker("network", "rm", resource_id)
                    except DockerRuntimeError as cleanup_error:
                        cleaned = False
                        exc.add_note(f"Rollback failed: {cleanup_error}")
            if cleaned and self.port_pool:
                self.port_pool.release(match_id)
            raise

    def inspect(self, match: Match) -> dict | None:
        match_id = self._match_id(match)
        containers = self._resources("container", match_id, role="target")
        if not containers:
            return None
        if len(containers) != 1:
            raise DockerRuntimeError("expected exactly one target for this match")
        return json.loads(self._docker("container", "inspect", containers[0]))[0]

    def start(self, match: Match) -> dict:
        target = self.inspect(match)
        if target is None:
            raise DockerRuntimeError("prepare the match before starting it")
        if target["State"]["Status"] != "created":
            raise DockerRuntimeError("target may only be started once; prepare a fresh arena")
        self._docker("start", target["Id"])
        target = self.inspect(match)
        if target is None or not target["State"]["Running"]:
            raise DockerRuntimeError("target exited during startup")
        return target

    def destroy(self, match: Match) -> None:
        match_id = self._match_id(match)
        for kind in ("container", "network", "volume"):
            resources = self._resources(kind, match_id)
            if resources:
                options = ["--force", "--volumes"] if kind == "container" else []
                self._docker(kind, "rm", *options, *resources)
        if self.port_pool:
            self.port_pool.release(match_id)

    def endpoints(self, target, case, internal_host):
        services = [(s.name, s.port, s.protocol) for s in case.required_services]
        services.append(("ssh", 22, "tcp"))
        result = []
        for name, port, protocol in services:
            host = internal_host
            if self.publish_ip:
                binding = target["NetworkSettings"]["Ports"][f"{port}/{protocol}"]
                if len(binding) != 1 or binding[0]["HostIp"] != self.publish_ip:
                    raise DockerRuntimeError("unexpected published endpoint")
                host, port = self.public_host, int(binding[0]["HostPort"])
                if not self.port_pool.first <= port <= self.port_pool.last or port in self.port_pool.reserved:
                    raise DockerRuntimeError("published endpoint outside game port policy")
            result.append(Endpoint(name=name, host=host, port=port, protocol=protocol))
        return result

    def cleanup_owned(self):
        for kind in ("container", "network", "volume"):
            args = [kind, "ls", "--quiet"] + (["--all"] if kind == "container" else [])
            resources = self._docker(*args, "--filter", "label=cyberrange=true",
                                     "--filter", f"label=arena_owner={self.owner}").split()
            if resources:
                options = ["--force", "--volumes"] if kind == "container" else []
                self._docker(kind, "rm", *options, *resources)
