#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose up --build -d --wait
docker compose exec -T backend python -m pytest -q
player_id=$(docker compose exec -T backend python - <<'PY'
import uuid
from sqlalchemy.orm import Session
from portal.app.database import make_engine
from portal.app.models import Player

with Session(make_engine()) as session:
    player = Player(nickname=f"checkpoint-01-{uuid.uuid4()}")
    session.add(player)
    session.flush()
    print(player.id)
    session.commit()
PY
)
docker compose restart backend
docker compose up -d --wait
docker compose exec -T backend python - "$player_id" <<'PY'
import sys
import urllib.request
from sqlalchemy.orm import Session
from portal.app.database import make_engine
from portal.app.models import Player

with urllib.request.urlopen("http://localhost:8000/health", timeout=5) as response:
    assert response.status == 200
with Session(make_engine()) as session:
    player = session.get(Player, int(sys.argv[1]))
    assert player is not None, "Player was lost after restart"
    assert player.nickname.startswith("checkpoint-01-")
    session.delete(player)
    session.commit()
print("HTTP health and database persistence: OK")
PY
docker compose down
printf '%s\n' 'CHECKPOINT 1 PASSED'
