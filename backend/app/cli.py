import argparse
from pathlib import Path

from .cases import CaseCatalog


def main():
    parser = argparse.ArgumentParser(prog="cyberrange")
    commands = parser.add_subparsers(dest="command", required=True)
    cases = commands.add_parser("cases").add_subparsers(dest="action", required=True)
    validate = cases.add_parser("validate")
    validate.add_argument("--directory", type=Path, default=Path("cases"))
    args = parser.parse_args()
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
