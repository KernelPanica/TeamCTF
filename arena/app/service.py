"""Disposable single-worker execution state shared by both providers."""
import asyncio
import hashlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import SecretStr
from shared.arena import ArenaError, ArenaEvent, ArenaStatus, EventPage, Observation


@dataclass
class Execution:
    fingerprint: str
    status: ArenaStatus
    task: asyncio.Task | None = None
    events: deque = field(default_factory=lambda: deque(maxlen=1000))
    sequence: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stop: asyncio.Event = field(default_factory=asyncio.Event)


class ArenaService:
    def __init__(self, executor, interval=2):
        self.executor = executor
        self.interval = interval
        self.instance_id = uuid4()
        self.matches = {}
        self.ready = False

    async def start(self):
        await asyncio.to_thread(self.executor.cleanup)
        self.ready = True

    async def close(self):
        self.ready = False
        errors = []
        for match_id in list(self.matches):
            try:
                await self.destroy_match(match_id)
            except ArenaError as error:
                errors.append(error)
        if errors:
            raise ArenaError("CLEANUP_FAILED")

    def execution(self, match_id):
        try:
            return self.matches[match_id]
        except KeyError:
            raise ArenaError("NOT_FOUND", 404) from None

    async def create_match(self, request):
        if not self.ready:
            raise ArenaError("NOT_READY")
        # No await until the entry is reserved: retries cannot create duplicates.
        fingerprint = hashlib.sha256((str(request.run_id) + "\0" + request.case_id + "\0" +
                                      request.red_key.get_secret_value()).encode()).hexdigest()
        existing = self.matches.get(request.match_id)
        if existing:
            if existing.fingerprint != fingerprint:
                raise ArenaError("CONFLICT", 409)
            return existing.status.model_copy(deep=True)
        entry = Execution(fingerprint, ArenaStatus(match_id=request.match_id, run_id=request.run_id,
                          instance_id=self.instance_id, state="PROVISIONING"))
        self.matches[request.match_id] = entry
        entry.task = asyncio.create_task(self.run(entry, request))
        return entry.status.model_copy(deep=True)

    async def run(self, entry, request):
        try:
            match, case, password, endpoints = await asyncio.to_thread(self.executor.create, request)
            if entry.stop.is_set():
                return
            entry.status.state = "READY"
            entry.status.blue_password = SecretStr(password)
            entry.status.endpoints = endpoints
            while not entry.stop.is_set():
                try:
                    observation = await asyncio.to_thread(self.executor.observe, match, case,
                                                          request.red_key.get_secret_value())
                except Exception:
                    observation = Observation(
                        health={s.name: {"status": "UNHEALTHY", "reason": "CHECK_ERROR"}
                                for s in case.required_services}, exploit="ERROR", surface="UNAVAILABLE",
                        red_exploit="ERROR", red_services={s.name: "UNAVAILABLE" for s in case.required_services},
                        duration_seconds=0,
                    )
                if entry.stop.is_set():
                    return
                entry.sequence += 1
                entry.events.append(ArenaEvent(sequence=entry.sequence,
                    timestamp=datetime.now(timezone.utc), observation=observation))
                try:
                    await asyncio.wait_for(entry.stop.wait(), timeout=self.interval)
                except TimeoutError:
                    pass
        except Exception:
            entry.status.state = "FAILED"
            entry.status.error = "EXECUTION_FAILED"
            entry.status.blue_password = None
            entry.status.endpoints = []
            try:
                await asyncio.to_thread(self.executor.destroy, request.match_id)
            except Exception:
                entry.status.error = "CLEANUP_FAILED"

    async def get_match(self, match_id):
        return self.execution(match_id).status.model_copy(deep=True)

    async def get_events(self, match_id, after=0):
        entry = self.execution(match_id)
        if after < 0 or after > entry.sequence:
            raise ArenaError("INVALID_CURSOR", 409)
        if entry.events and after < entry.events[0].sequence - 1:
            raise ArenaError("EVENT_GAP", 409)
        events = [event for event in entry.events if event.sequence > after][:100]
        return EventPage(run_id=entry.status.run_id, instance_id=self.instance_id,
                         events=events, cursor=events[-1].sequence if events else after)

    async def destroy_match(self, match_id):
        entry = self.matches.get(match_id)
        if entry is None:
            # No state can also mean a previous process crashed during creation.
            await asyncio.to_thread(self.executor.destroy, match_id)
            return
        async with entry.lock:
            if entry.status.state == "DELETED":
                return
            entry.stop.set()
            entry.status.state = "DELETING"
            entry.status.blue_password = None
            entry.status.endpoints = []
            # Never cancel a thread doing Docker work: wait before removing resources.
            if entry.task:
                await asyncio.shield(entry.task)
            try:
                await asyncio.to_thread(self.executor.destroy, match_id)
            except Exception:
                entry.status.state = "FAILED"
                entry.status.error = "CLEANUP_FAILED"
                raise ArenaError("CLEANUP_FAILED") from None
            entry.task = None
            entry.events.clear()
            entry.status.state = "DELETED"
            entry.status.error = None
            # Keep only a non-secret tombstone until restart for POST idempotency.
