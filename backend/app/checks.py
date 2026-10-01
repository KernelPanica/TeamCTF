"""Shared boundary checks for controller-side case scripts."""
import ipaddress
from pathlib import Path

from .cases import CaseLoader, CaseSpec
from .models import Match, MatchState


def checked_case_directory(match: Match, case: CaseSpec, root: Path) -> Path:
    if match.state != MatchState.RUNNING or not match.target_host:
        raise ValueError("checks require a running arena")
    if match.case_id != case.id:
        raise ValueError("checker must belong to the match case")
    root = Path(root).resolve()
    directory = root / case.id
    if directory.is_symlink() or not directory.resolve().is_relative_to(root):
        raise ValueError("case directory escapes the catalog")
    if CaseLoader().load(directory) != case:
        raise ValueError("case changed; reload the catalog")
    return directory


def target_url(host: str, port: int) -> str:
    try:
        address = ipaddress.ip_address(host)
        host = f"[{address}]" if address.version == 6 else str(address)
    except ValueError:
        # Docker DNS name, produced by the runtime rather than user input.
        if not host.startswith("range-match-") or not host.endswith("-target"):
            raise ValueError("invalid target host")
    return f"http://{host}:{port}"
