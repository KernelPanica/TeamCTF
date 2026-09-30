from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.database import make_engine
from backend.app.main import create_app
from backend.app.models import Case, Match, MatchEvent, MatchPlayer, MatchState, Player, Team
from backend.app.states import transition


@pytest.fixture
def database(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    engine = make_engine(url)
    yield url, engine, config
    engine.dispose()


def test_migrations_and_models(database):
    _, engine, config = database
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(engine) as session:
        player = Player(nickname="blue")
        case = Case(id="example", name="Example")
        session.add_all([player, case])
        session.flush()
        match = Match(case_id=case.id)
        session.add(match)
        session.flush()
        assert match.state == MatchState.WAITING
        session.add_all([
            MatchPlayer(match_id=match.id, player_id=player.id, team=Team.BLUE),
            MatchEvent(match_id=match.id, type="MATCH_CREATED", event_metadata={"test": True}),
        ])
        session.commit()
    command.upgrade(config, "head")
    command.check(config)
    with Session(engine) as session:
        assert session.get(Player, 1).nickname == "blue"
        assert session.get(MatchPlayer, (1, 1)).team == Team.BLUE
        event = session.get(MatchEvent, 1)
        assert event.event_metadata == {"test": True}
        assert before <= event.timestamp <= datetime.now(timezone.utc).replace(tzinfo=None)
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1


@pytest.mark.parametrize("sql", [
    "INSERT INTO players (nickname) VALUES ('same'), ('same')",
    "INSERT INTO matches (case_id) VALUES ('missing')",
    "INSERT INTO matches (state) VALUES ('UNKNOWN')",
    "INSERT INTO match_players (match_id, player_id) VALUES (999, 999)",
    "INSERT INTO match_players (match_id, player_id, team) VALUES (1, 1, 'GREEN')",
    "INSERT INTO match_players (match_id, player_id) VALUES (1, 1), (1, 1)",
])
def test_database_constraints(database, sql):
    _, engine, _ = database
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO players (id, nickname) VALUES (1, 'first')"))
        connection.execute(text("INSERT INTO matches (id) VALUES (1)"))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text(sql))


@pytest.mark.parametrize("source", list(MatchState))
@pytest.mark.parametrize("target", list(MatchState))
def test_transitions(source, target):
    allowed = {
        MatchState.WAITING: {MatchState.MATCHMAKING, MatchState.FAILED},
        MatchState.MATCHMAKING: {MatchState.PROVISIONING, MatchState.FAILED},
        MatchState.PROVISIONING: {MatchState.RUNNING, MatchState.FAILED},
        MatchState.RUNNING: {MatchState.FINISHING, MatchState.FAILED},
        MatchState.FINISHING: {MatchState.FINISHED, MatchState.FAILED},
    }
    match = Match(state=source)
    if target in allowed.get(source, set()):
        transition(match, target)
        assert match.state == target
    else:
        with pytest.raises(ValueError):
            transition(match, target)
        assert match.state == source


def test_health(database, tmp_path):
    url, _, _ = database
    with TestClient(create_app(url)) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    with TestClient(create_app(f"sqlite:///{tmp_path / 'missing' / 'db'}")) as client:
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
