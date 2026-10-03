import json
import socket
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from portal.app.analyzer import analyze_match
from portal.app.main import create_app
from portal.app.models import Match, MatchEvent, MatchState
from test_checkpoint_09 import lobby_db, auth
from test_checkpoint_12 import game


def sample():
    start = datetime(2026, 1, 1)
    match = Match(id=1, state=MatchState.FINISHED, started_at=start,
                  finished_at=start + timedelta(seconds=100), winner='BLUE')
    rows = [(0, 'MATCH_STARTED', {}), (10, 'SERVICE_UP', {'service': 'web'}),
            (20, 'SERVICE_DOWN', {'service': 'web'}), (25, 'SERVICE_DOWN', {'service': 'web'}),
            (40, 'SERVICE_RESTORED', {'service': 'web'}), (45, 'EXPLOIT_BLOCKED', {}),
            (45, 'BLUE_SECURING_STARTED', {}), (50, 'BLUE_SECURING_CANCELLED', {}),
            (55, 'EXPLOIT_RESTORED', {}), (60, 'BLUE_SECURING_STARTED', {}),
            (70, 'KEY_SUBMITTED_INVALID', {'team': 'RED'}), (90, 'SERVICE_DOWN', {'service': 'web'}),
            (95, 'BLUE_KEY_ISSUED', {}), (100, 'MATCH_FINISHED', {}),
            (110, 'ARENA_DESTROYED', {})]
    events = [MatchEvent(id=i + 1, match_id=1, type=kind, timestamp=start + timedelta(seconds=offset),
                         event_metadata=metadata) for i, (offset, kind, metadata) in enumerate(rows)]
    return match, events


def test_exact_metrics_and_determinism(monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail('Analyzer must not access the network')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    match, events = sample()
    report = analyze_match(match, events)
    assert report['duration_seconds'] == 100 and report['winner'] == 'BLUE'
    assert report['services']['web'] == {'uptime_seconds': 60, 'downtime_seconds': 30,
        'unknown_seconds': 10, 'longest_downtime_seconds': 20, 'uptime_percent': 60}
    assert report['time_to_exploit_blocked_seconds'] == 45
    assert report['securing_attempts'] == 2 and report['securing_cancellations'] == 1
    assert report['invalid_key_submissions'] == 1
    assert report['milestones'][2]['since_previous_seconds'] == 10
    assert report['milestones'][-1]['elapsed_seconds'] == 100
    encoded = json.dumps(report, sort_keys=True)
    assert json.dumps(analyze_match(match, list(reversed(events))), sort_keys=True) == encoded
    assert json.dumps(analyze_match(match, events), sort_keys=True) == encoded
    assert 'objective' not in str(report)


def test_missing_history_zero_duration_and_multiple_services():
    match, events = sample()
    assert analyze_match(match, [])['services'] == {}
    assert analyze_match(match, [])['time_to_exploit_blocked_seconds'] is None
    events.append(MatchEvent(id=99, match_id=1, type='SERVICE_DOWN', timestamp=match.started_at,
                             event_metadata={'service': 'other', 'downtime_seconds': 999999}))
    report = analyze_match(match, events)
    assert report['services']['other']['downtime_seconds'] == 100
    assert report['services']['other']['longest_downtime_seconds'] == 100
    match.finished_at = match.started_at
    assert analyze_match(match, events)['services']['other']['uptime_percent'] is None
    match.started_at = None
    with pytest.raises(ValueError):
        analyze_match(match, events)


def test_report_api_authorization_restart_and_no_secrets(lobby_db):
    url, engine = lobby_db
    match_id, keys, teams = game(lobby_db)
    endpoint = f'/matches/{match_id}/report'
    with TestClient(create_app(url)) as client:
        headers = auth(teams['RED'][0]['token'])
        assert client.get(endpoint, headers=headers).status_code == 409
        assert client.get(endpoint).status_code == 401
        outsider = client.post('/lobby/session', json={'nickname': 'outsider'}).json()
        assert client.get(endpoint, headers=auth(outsider['token'])).status_code == 404
        client.post(f'/matches/{match_id}/submit', headers=headers, json={'key': keys['RED']})
        response = client.get(endpoint, headers=headers)
        assert response.headers['cache-control'] == 'no-store'
        report = response.json()
        assert report['winner'] == 'RED' and report['duration_seconds'] >= 3661
        assert all(key not in str(report) for key in keys.values())
    with TestClient(create_app(url)) as restarted:
        for players in teams.values():
            for player in players:
                assert restarted.get(endpoint, headers=auth(player['token'])).json() == report
        with Session(engine) as session:
            match = session.get(Match, match_id)
            session.add(MatchEvent(match_id=match_id, type='ARENA_DESTROYED',
                timestamp=match.finished_at + timedelta(seconds=5), event_metadata={}))
            session.commit()
        assert restarted.get(endpoint, headers=headers).json() == report
