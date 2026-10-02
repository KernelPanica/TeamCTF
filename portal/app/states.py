from datetime import datetime, timezone

from .models import Match, MatchState


_SEQUENCE = list(MatchState)[:-1]


def transition(match: Match, target: MatchState) -> None:
    target = MatchState(target)
    current = match.state
    if current in (MatchState.FINISHED, MatchState.FAILED):
        raise ValueError(f"Cannot leave terminal state {current}")
    if target != MatchState.FAILED and target != _SEQUENCE[_SEQUENCE.index(current) + 1]:
        raise ValueError(f"Invalid transition: {current} -> {target}")
    match.state = target
    if target == MatchState.RUNNING:
        match.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
