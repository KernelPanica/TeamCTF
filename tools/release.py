"""Build allowlisted release contexts/wheel, then assemble digest-pinned archives.

prepare performs no Docker or registry operation. bundle requires real image
references from the image builder; it never guesses or manufactures digests.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shared.version import VERSION, API_VERSION


def copy_tree(source, target, suffixes=None):
    for path in sorted(source.rglob('*')):
        if '__pycache__' in path.parts:
            continue
        if path.is_symlink():
            raise ValueError(f'Symlink in release input: {path}')
        if path.is_file() and (suffixes is None or path.suffix in suffixes):
            dest = target / path.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)


def checksums(output):
    files = sorted(p for p in output.iterdir() if p.is_file() and p.name != 'SHA256SUMS')
    lines = []
    for path in files:
        with path.open('rb') as stream:
            lines.append(f'{hashlib.file_digest(stream, "sha256").hexdigest()}  {path.name}\n')
    (output / 'SHA256SUMS').write_text(''.join(lines))


def prepare(output):
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use a new empty output directory')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary)
        for name in ('portal/app', 'portal/migrations', 'shared'):
            copy_tree(ROOT / name, source / name, {'.py'})
        copy_tree(ROOT / 'portal/frontend', source / 'portal/frontend', {'.html', '.css', '.js'})
        shutil.copyfile(ROOT / 'portal/__init__.py', source / 'portal/__init__.py')
        shutil.copyfile(ROOT / 'portal/alembic.ini', source / 'portal/alembic.ini')
        # Preserve existing validation/case format without shipping Arena runtime.
        copy_tree(ROOT / 'arena/cases', source / 'portal/case_catalog')
        project = (ROOT / 'pyproject.toml').read_text().replace('"portal*", "arena*", "shared*"', '"portal*", "shared*"')
        project += '\n[tool.setuptools.package-data]\nportal = ["frontend/*", "alembic.ini", "migrations/*.py", "migrations/versions/*.py", "case_catalog/**/*"]\n'
        (source / 'pyproject.toml').write_text(project)
        subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                        '--wheel-dir', str(output), str(source)], check=True)
    portal = output / 'portal-context'
    portal.mkdir()
    wheel, = output.glob('*.whl')
    shutil.copyfile(wheel, portal / wheel.name)
    shutil.copyfile(ROOT / 'release/portal/Dockerfile', portal / 'Dockerfile')
    arena = output / 'arena-context'
    copy_tree(ROOT / 'arena/app', arena / 'arena/app', {'.py'})
    (arena / 'arena/__init__.py').write_text('')
    copy_tree(ROOT / 'shared', arena / 'shared', {'.py'})
    copy_tree(ROOT / 'arena/cases', arena / 'arena/cases')
    dockerfile = (ROOT / 'arena/Dockerfile').read_text()
    dockerfile += f'\nLABEL org.opencontainers.image.version="{VERSION}"\nENV ARENA_CASE_IMAGES=/app/arena/case-images.json\n'
    (arena / 'Dockerfile').write_text(dockerfile)
    copy_tree(ROOT / 'arena/cases/web-001', output / 'target-context')
    metadata = {'version': VERSION, 'api_version': API_VERSION, 'platform': 'linux/amd64',
                'status': 'candidate', 'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip())}
    (output / 'release.json').write_text(json.dumps(metadata, indent=2) + '\n')
    shutil.copyfile(ROOT / 'release/README.md', output / 'RELEASE_NOTES.md')
    shutil.copyfile(ROOT / 'tools/accept_release.py', output / 'accept_release.py')
    shutil.copyfile(ROOT / 'tools/native_smoke.py', output / 'native_smoke.py')
    checksums(output)


def bundle(output, references):
    metadata = json.loads((output / 'release.json').read_text())
    if metadata['version'] != VERSION:
        raise ValueError('Context version mismatch')
    case_images = json.loads((output / 'arena-context/arena/case-images.json').read_text())
    if set(case_images) != {'web-001'} or not re.fullmatch(r'[a-z0-9./:_-]+@sha256:[0-9a-f]{64}', case_images['web-001']):
        raise ValueError('Target image digest is required')
    metadata['case_images'] = case_images
    for role, reference in references.items():
        if not re.fullmatch(r'[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}', reference):
            raise ValueError('Images must be pinned repository@sha256 references')
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / role
            directory.mkdir()
            template = ROOT / 'release' / role
            (directory / 'compose.yaml').write_text((template / 'compose.yaml').read_text().replace('__IMAGE__', reference))
            shutil.copyfile(template / '.env.example', directory / '.env.example')
            shutil.copyfile(ROOT / 'release/README.md', directory / 'README.md')
            if role == 'arena':
                shutil.copyfile(ROOT / 'arena/app/runtime/firewall.py', directory / 'firewall.py')
            (directory / 'release.json').write_text(json.dumps(metadata | {'image': reference}, indent=2) + '\n')
            with tarfile.open(output / f'{role}-v{VERSION}.tar.gz', 'w:gz') as archive:
                archive.add(directory, arcname=role)
    metadata['images'] = references
    (output / 'release.json').write_text(json.dumps(metadata, indent=2) + '\n')
    checksums(output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'bundle'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--portal-image')
    parser.add_argument('--arena-image')
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args.output.resolve())
    else:
        if not args.portal_image or not args.arena_image:
            parser.error('bundle requires both digest-pinned images')
        bundle(args.output.resolve(), {'portal': args.portal_image, 'arena': args.arena_image})


if __name__ == '__main__':
    main()
