"""Run against a wheel already installed in an independent Python environment."""
import argparse
import os
import subprocess
import tempfile
from pathlib import Path


SMOKE = '''
import importlib.util
import shutil
import sys
from fastapi.testclient import TestClient
from portal.app.cli import main
from portal.app.main import create_app
assert shutil.which('docker') is None
assert importlib.util.find_spec('arena') is None
sys.argv = ['cyberrange', 'migrate']
assert main() == 0
sys.argv = ['cyberrange', 'cases', 'validate']
assert main() == 0
with TestClient(create_app()) as client:
    assert client.get('/health').json() == {'status': 'ok'}
    assert client.get('/version').json() == {'version': '0.1.0', 'arena_api_version': 1}
    assert 'report-timeline' in client.get('/').text
    assert client.get('/static/app.js').status_code == 200
    player = client.post('/lobby/session', json={'nickname': 'native-smoke'}).json()
    headers = {'Authorization': 'Bearer ' + player['token']}
    assert client.post('/lobby/queue', headers=headers).status_code == 200
with TestClient(create_app()) as restarted:
    assert restarted.get('/lobby/session', headers=headers).json()['status'] == 'SEARCHING'
print('NATIVE WHEEL SMOKE PASSED: migrations, case catalog, frontend, API, persistence; no Docker/Arena package')
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', type=Path, required=True)
    args = parser.parse_args()
    interpreter = args.python.absolute()
    with tempfile.TemporaryDirectory() as temporary:
        env = {k: v for k, v in os.environ.items() if not k.startswith(('ARENA_', 'PORTAL_', 'PYTHON'))}
        env.update(DATABASE_URL=f'sqlite:///{temporary}/portal.db', PATH=str(interpreter.parent))
        subprocess.run([str(interpreter), '-I', '-c', SMOKE], cwd=temporary, env=env, check=True)


if __name__ == '__main__':
    main()
