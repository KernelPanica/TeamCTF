"""Adapters for pre-split component tests; production code uses async providers.

These wire the relocated checker to Portal persistence without reproducing
checker algorithms. The 8A suite separately exercises the real Agent lifecycle.
"""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from shared.arena import ArenaStatus, ArenaEvent, EventPage, Observation
from arena.app.checks.health import HealthChecker as RuntimeHealth
from arena.app.checks.exploit import ExploitChecker
from arena.app.checks.red_zone import RedZoneChecker
from arena.app.runtime.executor import Executor
from portal.app import health, provisioning, exploit, red_zone, victory


class HealthChecker(RuntimeHealth):
    def poll(self, session, match, case):
        result = health.record_health(session, match, self.check(match, case))
        session.commit()
        return result


class ExecutionProvider:
    """Synchronous-runtime test double of the provider transport."""
    def __init__(self, runtime, directory=Path("arena/cases")):
        self.executor = Executor(directory, runtime)

    async def create_match(self, request):
        match, case, password, endpoints = self.executor.create(request)
        return ArenaStatus(match_id=request.match_id, run_id=request.run_id, instance_id=uuid4(),
                           state="READY", blue_password=password, endpoints=endpoints)

    async def destroy_match(self, match_id):
        self.executor.destroy(match_id)


def provision_arena(session, match, case, runtime):
    provider = ExecutionProvider(runtime)
    try:
        return asyncio.run(provisioning.provision_arena(session, match, case, provider))
    except Exception:
        # The direct executor double has no asynchronous FAILED status.
        from portal.app.models import MatchState
        match.state = MatchState.FAILED
        match.red_key = match.blue_key = None
        session.commit()
        raise


def destroy_arena(session, match, runtime):
    return asyncio.run(provisioning.destroy_arena(session, match, ExecutionProvider(runtime)))


class ObservationProvider:
    def __init__(self, match, observation):
        if not match.arena_run_id:
            match.arena_run_id = str(uuid4())
            match.arena_instance_id = str(uuid4())
        self.match, self.observation = match, observation

    async def get_match(self, match_id):
        return ArenaStatus(match_id=match_id, run_id=self.match.arena_run_id,
                           instance_id=self.match.arena_instance_id, state="READY")

    async def get_events(self, match_id, after=0):
        event = ArenaEvent(sequence=after + 1, timestamp=datetime.now(timezone.utc), observation=self.observation)
        return EventPage(run_id=self.match.arena_run_id, instance_id=self.match.arena_instance_id,
                         events=[event], cursor=after + 1)


def observation_provider(session, match, case, key, directory, runtime=None):
    services = RuntimeHealth(directory).check(match, case)
    direct = ExploitChecker(directory).check(match, case, key)
    red = RedZoneChecker(runtime, directory).check(match, case, key) if runtime is not None else {
        "surface": "AVAILABLE", "exploit": direct, "services": {s.name: "AVAILABLE" for s in case.required_services}}
    provider = ObservationProvider(match, Observation(health=services, exploit=direct,
        surface=red["surface"], red_exploit=red["exploit"], red_services=red["services"], duration_seconds=0))
    session.commit()
    return provider


def check_defense(session, match, case, key, directory):
    provider = observation_provider(session, match, case, key, directory)
    return asyncio.run(exploit.check_defense(session, match, provider))


def check_red_defense(session, match, case, key, runtime, directory):
    provider = observation_provider(session, match, case, key, directory, runtime)
    return asyncio.run(red_zone.check_red_defense(session, match, provider))


class VictoryEngine(victory.VictoryEngine):
    def poll(self, session, match, case):
        from arena.app.runtime.docker import DockerRuntime
        if isinstance(self.provider, DockerRuntime):
            runtime = self.provider
            self.provider = observation_provider(session, match, case, match.red_key, self.cases_directory, runtime)
            try:
                return asyncio.run(super().poll(session, match, case))
            finally:
                self.provider = runtime
        return asyncio.run(super().poll(session, match, case))
