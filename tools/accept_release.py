"""Black-box RED and BLUE games against an installed release (stdlib + ssh)."""
import argparse
import json
import os
import secrets
import shlex
import socket
import ssl
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--portal-url', required=True)
    parser.add_argument('--ca-file')
    parser.add_argument('--output', type=Path, default=Path('acceptance.json'))
    args = parser.parse_args()
    context = ssl.create_default_context(cafile=args.ca_file)

    def api(path, player=None, data=None):
        headers = {'Content-Type': 'application/json'}
        if player:
            headers['Authorization'] = 'Bearer ' + player['token']
        request = Request(args.portal_url.rstrip('/') + path,
            data=json.dumps(data).encode() if data is not None else None, headers=headers)
        with urlopen(request, context=context, timeout=15) as response:
            return json.load(response)

    def wait(read, predicate, seconds=900):
        deadline = time.monotonic() + seconds
        while True:
            value = read()
            if predicate(value):
                return value
            if time.monotonic() >= deadline:
                raise RuntimeError('Acceptance timed out')
            time.sleep(1)

    with tempfile.TemporaryDirectory() as temporary:
        assert api('/version') == {'version': '0.1.0', 'arena_api_version': 1}
        directory = Path(temporary)
        askpass = directory / 'askpass'
        askpass.write_text("#!/usr/bin/env python3\nimport os\nprint(os.environ['ACCEPT_SSH_PASSWORD'])\n")
        askpass.chmod(0o700)

        def ssh(access, command):
            known_hosts = directory / f'known_hosts-{match_id}'
            result = subprocess.run(['ssh', '-F', '/dev/null', '-p', str(access['port']),
                '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={known_hosts}',
                '-o', 'ConnectTimeout=5', '-o', 'NumberOfPasswordPrompts=1',
                '-o', 'PreferredAuthentications=password', '-o', 'PubkeyAuthentication=no',
                f'{access["username"]}@{access["host"]}', command],
                env=os.environ | {'SSH_ASKPASS': str(askpass), 'SSH_ASKPASS_REQUIRE': 'force',
                                   'DISPLAY': ':0', 'ACCEPT_SSH_PASSWORD': access['password']},
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise RuntimeError('BLUE SSH command failed')
            return result.stdout

        prefix = secrets.token_hex(4)
        players = [api('/lobby/session', data={'nickname': f'accept-{prefix}-{i}'}) for i in range(4)]
        evidence = []
        for winner in ('RED', 'BLUE'):
            for player in players:
                api('/lobby/queue', player, {})
            state = wait(lambda: api('/lobby/session', players[0]),
                         lambda s: s.get('match', {}).get('state') in ('RUNNING', 'FAILED'))
            assert state['match']['state'] == 'RUNNING'
            match_id = state['match']['id']
            states = [api('/lobby/session', player)['match'] for player in players]
            assert {s['id'] for s in states} == {match_id}
            assert sorted(s['team'] for s in states) == ['BLUE', 'BLUE', 'RED', 'RED']
            assert all(s['case_id'] == 'web-001' for s in states)
            red = next(p for p, s in zip(players, states) if s['team'] == 'RED')
            blue = next(p for p, s in zip(players, states) if s['team'] == 'BLUE')
            access_path = f'/matches/{match_id}/access'
            red_access, blue_access = api(access_path, red), api(access_path, blue)
            assert 'password' not in red_access and 'blue_key' not in red_access
            assert '20.04' in ssh(blue_access, 'cat /etc/os-release')
            assert ssh(blue_access, 'sudo -n id -u').strip() == '0'
            web = next(e for e in red_access['services'] if e['name'] == 'web')
            address = f'http://{web["host"]}:{web["port"]}'
            assert api(f'/matches/{match_id}/submit', red, {'key': 'wrong'})['result'] == 'INVALID'
            if winner == 'RED':
                with urlopen(address + '/download?' + urlencode({'path': '/opt/objective/red-key'}), timeout=5) as response:
                    key = response.read(4096).decode()
                assert key.startswith('RED_')
                player = red
            else:
                with urlopen(address + '/download?' + urlencode({'path': '/opt/objective/red-key'}), timeout=5) as response:
                    assert response.read(4096).startswith(b'RED_')  # Fresh vulnerable target for the second game.
                patch = ("from pathlib import Path; p=Path('/opt/service/server.py'); "
                         "s=p.read_text(); old='elif url.path == \"/download\":'; "
                         "assert old in s; p.write_text(s.replace(old, 'elif False:'))")
                # Fixed case-specific patch, executed only through BLUE's normal SSH access.
                ssh(blue_access, shlex.join(['sudo', '-n', 'python3', '-c', patch]) + ' && sudo -n service cyberrange-web restart')
                issued = wait(lambda: api(access_path, blue), lambda a: bool(a.get('blue_key')), seconds=180)
                key, player = issued['blue_key'], blue
                with urlopen(address + '/', timeout=5) as response:
                    assert response.read() == b'Cyber Range file service\n'
            assert api(f'/matches/{match_id}/submit', player, {'key': key})['result'] == winner + '_WIN'
            reports = [api(f'/matches/{match_id}/report', p) for p in players]
            assert all(report == reports[0] for report in reports) and reports[0]['winner'] == winner
            def destroyed():
                cursor = 0
                while True:
                    page = api(f'/matches/{match_id}/events?after={cursor}&limit=500', red)
                    if any(e['type'] == 'ARENA_DESTROYED' for e in page['events']):
                        return True
                    if len(page['events']) < 500:
                        return False
                    cursor = page['cursor']
            wait(destroyed, bool, seconds=60)
            for host, port in [(web['host'], web['port']), (blue_access['host'], blue_access['port'])]:
                try:
                    with socket.create_connection((host, port), timeout=2):
                        raise RuntimeError('Target endpoint still reachable after cleanup')
                except OSError:
                    pass
            evidence.append({'match_id': match_id, 'winner': winner, 'report': reports[0], 'cleanup': True})
        args.output.write_text(json.dumps({'scope': 'installed-release-game-scenarios', 'games': evidence}, indent=2) + '\n')
        print('RED/BLUE release games passed; two-host installation, restart and upgrade evidence is still required.')


if __name__ == '__main__':
    main()
