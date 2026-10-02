from dataclasses import dataclass


@dataclass
class RuntimeMatch:
    id: int
    case_id: str | None = None
    target_host: str | None = None
    state: str = "RUNNING"
    blue_login_seen: bool = False
