import argparse
import asyncio
import json
import os
from pathlib import Path

from .cases import CaseCatalog, DEFAULT_CASES
from .database import make_engine
from .health import watch_health
from .victory import watch_victory
from .arena_provider import configured_provider
from shared.arena import ArenaError
from .arena_reconcile import reconcile_once


async def run_watcher(engine, args):
    async with configured_provider() as provider:
        if provider is None:
            raise ArenaError("ARENA_NOT_CONFIGURED")
        watcher = watch_health if args.command == "health" else watch_victory
        async for result in watcher(engine, args.match_id, provider, args.interval):
            print(json.dumps(result), flush=True)


async def recover(engine):
    # Opening LocalArenaProvider performs destructive startup cleanup; recovery
    # of a live Agent must use the remote transport exclusively.
    if os.environ.get('ARENA_PROVIDER', 'remote') != 'remote':
        raise ValueError('Portal recovery requires RemoteArenaProvider')
    async with configured_provider() as provider:
        if provider is None:
            raise ArenaError('ARENA_NOT_CONFIGURED')
        pending = await reconcile_once(engine, provider)
        if pending:
            raise ArenaError(f'RECOVERY_PENDING: {pending}')
        print('Portal arena recovery completed')


def main():
    parser = argparse.ArgumentParser(prog="cyberrange")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser('migrate')
    commands.add_parser('arena-check')
    recovery = commands.add_parser('portal').add_subparsers(dest='area', required=True)
    recovery.add_parser('arena').add_argument('action', choices=['recovery'])
    cases = commands.add_parser("cases").add_subparsers(dest="action", required=True)
    validate = cases.add_parser("validate")
    validate.add_argument("--directory", type=Path, default=DEFAULT_CASES)
    for name in ("health", "victory"):
        actions = commands.add_parser(name).add_subparsers(dest="action", required=True)
        watch = actions.add_parser("watch")
        watch.add_argument("match_id", type=int)
        watch.add_argument("--directory", type=Path, default=Path("arena/cases"))
        watch.add_argument("--interval", type=float, default=2)
    args = parser.parse_args()
    if args.command == 'migrate':
        from alembic import command
        from alembic.config import Config
        config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
        command.upgrade(config, 'head')
        return 0
    if args.command == 'arena-check':
        async def check():
            if os.environ.get('ARENA_PROVIDER', 'remote') != 'remote':
                raise ValueError('arena-check requires RemoteArenaProvider')
            async with configured_provider() as provider:
                if provider is None:
                    raise ArenaError('ARENA_NOT_CONFIGURED')
                info = await provider.check_compatibility()
                print(f'Arena {info.version}, API {info.api_version}: READY')
        try:
            asyncio.run(check())
        except (ArenaError, ValueError, OSError) as error:
            print(f'ERROR: {error}')
            return 1
        return 0
    if args.command in ("health", "victory", "portal"):
        engine = make_engine()
        try:
            asyncio.run(recover(engine) if args.command == 'portal' else run_watcher(engine, args))
        except (OSError, ValueError, ArenaError) as exc:
            print(f"ERROR: {exc}")
            return 1
        except KeyboardInterrupt:
            return 130
        finally:
            engine.dispose()
        return 0
    try:
        catalog = CaseCatalog(args.directory)
    except OSError as exc:
        print(f"INVALID: {exc}")
        return 1
    for case in catalog.list():
        print(f"{case.id}: READY")
    for id, reason in catalog.invalid.items():
        print(f"{id}: INVALID — {reason}")
    if not catalog.list() and not catalog.invalid:
        print("INVALID: no cases found")
    return int(bool(catalog.invalid) or not catalog.list())


if __name__ == "__main__":
    raise SystemExit(main())
