"""Deterministic measurements from persisted Portal state; no I/O or clock reads."""
from .models import MatchState


def analyze_match(match, events):
    start, end = match.started_at, match.finished_at
    if match.state != MatchState.FINISHED or start is None or end is None or end < start:
        raise ValueError('A finished match with valid start/end timestamps is required')
    seconds = lambda delta: round(delta.total_seconds(), 6)
    duration = seconds(end - start)
    events = sorted((e for e in events if e.match_id == match.id and start <= e.timestamp <= end),
                    key=lambda e: (e.timestamp, e.id))
    services = {}
    health_types = {'SERVICE_UP', 'SERVICE_DOWN', 'SERVICE_RESTORED'}
    names = sorted({e.event_metadata['service'] for e in events if e.type in health_types})
    for name in names:
        totals = {'up': 0.0, 'down': 0.0, 'unknown': 0.0}
        state, since, down_since, longest = 'unknown', start, None, 0.0
        for event in [e for e in events if e.type in health_types and e.event_metadata['service'] == name]:
            totals[state] += seconds(event.timestamp - since)
            new_state = 'down' if event.type == 'SERVICE_DOWN' else 'up'
            if state == 'down' and new_state != 'down':
                longest = max(longest, seconds(event.timestamp - down_since))
                down_since = None
            if new_state == 'down' and state != 'down':
                down_since = event.timestamp
            state, since = new_state, event.timestamp
        totals[state] += seconds(end - since)
        if down_since is not None:
            longest = max(longest, seconds(end - down_since))
        services[name] = {'uptime_seconds': round(totals['up'], 6),
                          'downtime_seconds': round(totals['down'], 6),
                          'unknown_seconds': round(totals['unknown'], 6),
                          'longest_downtime_seconds': longest,
                          'uptime_percent': round(100 * totals['up'] / duration, 6) if duration else None}
    blocked = next((e for e in events if e.type == 'EXPLOIT_BLOCKED'), None)
    attempts = sum(e.type == 'BLUE_SECURING_STARTED' for e in events)
    cancellations = sum(e.type == 'BLUE_SECURING_CANCELLED' for e in events)
    invalid = [e for e in events if e.type == 'KEY_SUBMITTED_INVALID']
    # Exclude raw checker payloads, which are not public milestones or proof of
    # the time at which a human RED player obtained the objective.
    milestones, previous = [], start
    kinds = {'MATCH_STARTED', 'TARGET_HEALTHY', 'BLUE_FIRST_LOGIN', *health_types,
             'EXPLOIT_AVAILABLE', 'EXPLOIT_BLOCKED', 'EXPLOIT_RESTORED',
             'BLUE_SECURING_STARTED', 'BLUE_SECURING_CANCELLED', 'BLUE_KEY_ISSUED',
             'KEY_SUBMITTED_INVALID', 'KEY_SUBMITTED_RED', 'KEY_SUBMITTED_BLUE', 'MATCH_FINISHED'}
    for event in events:
        if event.type in kinds:
            milestones.append({'event_id': event.id, 'type': event.type,
                'elapsed_seconds': seconds(event.timestamp - start),
                'since_previous_seconds': seconds(event.timestamp - previous)})
            previous = event.timestamp
    first_blocked = seconds(blocked.timestamp - start) if blocked else None
    notes = {'BLUE': [f'Попыток стабилизации: {attempts}.', f'Отмен стабилизации: {cancellations}.'],
             'RED': [f'Неверных отправок ключа RED: {sum(e.event_metadata.get("team") == "RED" for e in invalid)}.'],
             'SERVICE': [f'{name}: недоступность по журналу — {data["downtime_seconds"]:g} с; '
                         f'без наблюдений в начале — {data["unknown_seconds"]:g} с.'
                         for name, data in services.items()]}
    if first_blocked is not None:
        notes['BLUE'].append(f'Первая зафиксированная блокировка exploit — через {first_blocked:g} с.')
    return {'version': 1, 'match_id': match.id, 'winner': match.winner,
            'duration_seconds': duration, 'services': services,
            'time_to_exploit_blocked_seconds': first_blocked,
            'securing_attempts': attempts, 'securing_cancellations': cancellations,
            'invalid_key_submissions': len(invalid), 'milestones': milestones, 'notes': notes}
