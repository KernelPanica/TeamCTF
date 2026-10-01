"""Internal controller operations; not publicly callable provisioning endpoints."""
import secrets
import socket
import time

from sqlalchemy.orm import Session

from .cases import CaseSpec
from .models import Case, Match, MatchState
from .runtime import DockerRuntime, DockerRuntimeError
from .states import transition


def _wait_for_ssh(host: str) -> None:
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


def provision_arena(session: Session, match: Match, case: CaseSpec, runtime: DockerRuntime) -> None:
    if match.state != MatchState.PROVISIONING:
        raise ValueError("match must be PROVISIONING")
    if match.case_id is not None and match.case_id != case.id:
        raise ValueError("case does not match the assigned match case")
    # Refuse to take ownership of an existing arena, including one from another attempt.
    if runtime.inspect(match) is not None:
        raise DockerRuntimeError("match already has a target")
    prepared = False
    try:
        if session.get(Case, case.id) is None:
            session.add(Case(id=case.id, name=case.name))
            session.flush()
        match.case_id = case.id
        runtime.prepare(match, case)
        prepared = True
        target = runtime.start(match)
        match.red_key = "RED_" + secrets.token_urlsafe(32)
        match.blue_key = "BLUE_" + secrets.token_urlsafe(32)
        match.securing_started_at = match.blue_key_issued_at = None
        runtime._docker(
            "exec", "--interactive", target["Id"], "python3", "-c",
            "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
            "p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_bytes(sys.stdin.buffer.read()); p.chmod(0o444)",
            case.red.objective_path, stdin=match.red_key,
        )
        password = secrets.token_urlsafe(32)
        runtime._docker("exec", "--interactive", target["Id"], "chpasswd", stdin=f"blue:{password}\n")
        networks = target["NetworkSettings"]["Networks"]
        host = next(iter(networks.values()))["IPAddress"]
        _wait_for_ssh(host)
        match.target_host = host
        match.blue_password = password
        transition(match, MatchState.RUNNING)
        session.commit()
    except Exception as exc:
        session.rollback()
        match.target_host = match.blue_password = None
        match.red_key = match.blue_key = None
        match.securing_started_at = match.blue_key_issued_at = None
        transition(match, MatchState.FAILED)
        session.commit()
        if prepared:
            try:
                runtime.destroy(match)
            except DockerRuntimeError as cleanup_error:
                exc.add_note(f"Cleanup failed: {cleanup_error}")
        raise


def destroy_arena(session: Session, match: Match, runtime: DockerRuntime) -> None:
    # Revoke API access even if Docker is unavailable; cleanup errors remain visible.
    match.target_host = match.blue_password = None
    match.red_key = match.blue_key = None
    match.securing_started_at = match.blue_key_issued_at = None
    session.commit()
    runtime.destroy(match)
