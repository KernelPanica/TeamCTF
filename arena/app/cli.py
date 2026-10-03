"""Offline orphan cleanup. Stop the Agent before invoking this command."""
import argparse
import os

from .runtime.docker import DockerRuntime, DockerRuntimeError


def main():
    parser = argparse.ArgumentParser(prog='arena')
    parser.add_argument('command', choices=['reconcile'])
    parser.parse_args()
    owner = os.environ.get('ARENA_OWNER')
    if not owner:
        parser.error('ARENA_OWNER is required; stop the Agent before reconciliation')
    try:
        DockerRuntime(owner=owner).cleanup_owned()
    except DockerRuntimeError as error:
        print(f'ERROR: {error}')
        return 1
    print('Arena owner resources reconciled')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
