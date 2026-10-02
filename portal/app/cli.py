import argparse
import asyncio
import json
from pathlib import Path

from .cases import CaseCatalog
from .database import make_engine
from .health import watch_health
from .victory import watch_victory
from .arena_provider import configured_provider
from shared.arena import ArenaError


async def run_watcher(engine, args):
    async with configured_provider() as provider:
        if provider is None:
            raise ArenaError("ARENA_NOT_CONFIGURED")
        watcher = watch_health if args.command == "health" else watch_victory
        async for result in watcher(engine, args.match_id, provider, args.interval):
            print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(prog="cyberrange")
    commands = parser.add_subparsers(dest="command", required=True)
    cases = commands.add_parser("cases").add_subparsers(dest="action", required=True)
    validate = cases.add_parser("validate")
    validate.add_argument("--directory", type=Path, default=Path("arena/cases"))
    for name in ("health", "victory"):
        actions = commands.add_parser(name).add_subparsers(dest="action", required=True)
        watch = actions.add_parser("watch")
        watch.add_argument("match_id", type=int)
        watch.add_argument("--directory", type=Path, default=Path("arena/cases"))
        watch.add_argument("--interval", type=float, default=2)
    args = parser.parse_args()
    if args.command in ("health", "victory"):
        engine = make_engine()
        try:
            asyncio.run(run_watcher(engine, args))
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
