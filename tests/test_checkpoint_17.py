import asyncio
import hashlib
import json
import subprocess
import sys
import tarfile
import zipfile
from uuid import uuid4

import httpx
import pytest
import yaml

from portal.app.arena_provider import RemoteArenaProvider
from shared.arena import ArenaError, CreateMatch
from shared.version import VERSION
from tools.release import prepare, bundle, ROOT


@pytest.fixture(scope='module')
def candidate(tmp_path_factory):
    output = tmp_path_factory.mktemp('candidate')
    prepare(output)
    return output


def test_release_wheel_is_complete_and_roles_are_separated(candidate):
    wheel, = candidate.glob('*.whl')
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        for required in ('portal/frontend/index.html', 'portal/frontend/app.js',
                         'portal/alembic.ini', 'portal/migrations/env.py',
                         'portal/migrations/versions/0009_report.py',
                         'portal/case_catalog/web-001/case.yaml',
                         'portal/case_catalog/web-001/Dockerfile',
                         'portal/case_catalog/web-001/checks/health.py', 'shared/arena.py'):
            assert required in names
        assert not any(n.startswith('arena/') for n in names)
        assert not any(n.endswith(('.db', '.key', '.pem', '.env')) for n in names)
        assert f'Version: {VERSION}' in archive.read(f'cyberrange-{VERSION}.dist-info/METADATA').decode()
    arena = candidate / 'arena-context'
    assert (arena / 'arena/app/runtime/docker.py').is_file()
    assert not (arena / 'portal').exists()
    assert not list(arena.rglob('*.db'))


def test_deployment_archives_pin_digests_and_verify_checksums(candidate):
    (candidate / 'arena-context/arena/case-images.json').write_text(json.dumps({'web-001': 'registry.test/range/target@sha256:' + 'c' * 64}))
    references = {role: f'registry.test/range/{role}@sha256:' + character * 64
                  for role, character in [('portal', 'a'), ('arena', 'b')]}
    bundle(candidate, references)
    for role in references:
        with tarfile.open(candidate / f'{role}-v{VERSION}.tar.gz') as archive:
            assert all(not n.startswith('/') and '..' not in n.split('/') for n in archive.getnames())
            compose = yaml.safe_load(archive.extractfile(f'{role}/compose.yaml').read())
            service, = compose['services'].values()
            assert service['image'] == references[role] and 'build' not in service
            assert not any('../' in str(v) for v in service['volumes'])
            assert archive.extractfile(f'{role}/.env.example')
            if role == 'arena':
                assert archive.extractfile('arena/firewall.py')
            else:
                assert 'docker.sock' not in str(service)
    for line in (candidate / 'SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ')
        assert hashlib.sha256((candidate / name).read_bytes()).hexdigest() == digest
    with pytest.raises(ValueError, match='pinned'):
        bundle(candidate, {'portal': 'registry.test/portal:latest'})


@pytest.mark.parametrize('info', [
    {'api_version': 2, 'version': VERSION, 'ready': True},
    {'api_version': 1, 'version': '99.0.0', 'ready': True},
    None,
])
def test_incompatible_agent_rejected_before_post(info):
    requests = []
    def respond(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(404 if info is None else 200, json=info or {})
    async def scenario():
        provider = RemoteArenaProvider('https://arena.test', 'a' * 40, transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(ArenaError, match='INCOMPATIBLE_ARENA_VERSION'):
                await provider.create_match(CreateMatch(match_id=1, run_id=uuid4(), case_id='web-001', red_key='secret'))
        finally:
            await provider.close()
    asyncio.run(scenario())
    assert requests == [('GET', '/v1/info')]


def test_release_scripts_compile_and_shell_syntax():
    for path in (ROOT / 'tools').glob('*.py'):
        compile(path.read_text(), str(path), 'exec')
    subprocess.run(['bash', '-n', str(ROOT / 'tools/build_release.sh')], check=True)


@pytest.mark.parametrize('cached', [False, True])
def test_release_target_uses_digest_without_build(monkeypatch, cached):
    from arena.app.runtime.docker import DockerRuntime, DockerRuntimeError
    from arena.app.runtime.context import RuntimeMatch
    from shared.cases import CaseLoader
    reference = 'registry.test/target@sha256:' + 'a' * 64
    runtime = DockerRuntime(ROOT / 'arena/cases', case_images={'web-001': reference})
    calls = []
    missing = [not cached]
    def docker(*args, **kwargs):
        calls.append(args)
        if args[1] == 'ls':
            return ''
        if args[:2] == ('image', 'inspect'):
            if missing[0]:
                missing[0] = False
                raise DockerRuntimeError('not cached')
            return json.dumps([{'Config': {'Volumes': None}}])
        if args[0] == 'pull':
            return ''
        if args[:2] == ('network', 'create'):
            return 'network-id'
        if args[0] == 'create':
            return 'container-id'
        pytest.fail(f'Unexpected Docker command: {args}')
    monkeypatch.setattr(runtime, '_docker', docker)
    monkeypatch.setattr(runtime, 'inspect', lambda match: {'Id': 'container-id'})
    runtime.prepare(RuntimeMatch(1), CaseLoader().load(ROOT / 'arena/cases/web-001'))
    assert not any(call[0] == 'build' for call in calls)
    assert (('pull', reference) in calls) == (not cached)
    assert next(call for call in calls if call[0] == 'create')[-1] == reference
    with pytest.raises(ValueError, match='pinned'):
        DockerRuntime(case_images={'web-001': 'target:latest'})
