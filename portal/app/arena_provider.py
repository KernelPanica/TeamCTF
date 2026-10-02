"""Portal transport only. Runtime is imported lazily for explicit local mode."""
import os
import ssl
import json
from contextlib import asynccontextmanager

import httpx
from pydantic import ValidationError
from shared.arena import ArenaProvider, ArenaError, ArenaStatus, EventPage


class RemoteArenaProvider(ArenaProvider):
    def __init__(self, url, token, *, ca_file=None, transport=None):
        if not url.startswith("https://") or len(token) < 32:
            raise ValueError("Arena requires HTTPS and a dedicated token of at least 32 characters")
        self.client = httpx.AsyncClient(base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            verify=ssl.create_default_context(cafile=ca_file), follow_redirects=False,
            trust_env=False, timeout=httpx.Timeout(15, connect=5), transport=transport)

    async def close(self):
        await self.client.aclose()

    async def request(self, method, path, model=None, **kwargs):
        try:
            async with self.client.stream(method, path, **kwargs) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 2_000_000:
                        raise ArenaError("INVALID_RESPONSE")
                if not response.is_success:
                    codes = {404: "NOT_FOUND", 409: "CONFLICT", 401: "UNAUTHORIZED"}
                    code = codes.get(response.status_code, "ARENA_REQUEST_FAILED")
                    if response.headers.get("content-type", "").startswith("application/json"):
                        payload = json.loads(data)
                        reported = payload.get("code") if isinstance(payload, dict) else None
                        if reported in {"NOT_FOUND", "CONFLICT", "INVALID_CURSOR", "EVENT_GAP", "NOT_READY", "CLEANUP_FAILED", "INVALID_REQUEST"}:
                            code = reported
                    raise ArenaError(code, response.status_code)
                return model.model_validate_json(data) if model else None
        except (httpx.HTTPError, ValidationError, ValueError):
            raise ArenaError("ARENA_UNAVAILABLE_OR_INVALID") from None

    async def create_match(self, request):
        body = request.model_dump(mode="json")
        body["red_key"] = request.red_key.get_secret_value()
        return await self.request("POST", "/v1/matches", ArenaStatus, json=body)

    async def get_match(self, match_id):
        return await self.request("GET", f"/v1/matches/{match_id}", ArenaStatus)

    async def get_events(self, match_id, after=0):
        return await self.request("GET", f"/v1/matches/{match_id}/events", EventPage, params={"after": after})

    async def destroy_match(self, match_id):
        await self.request("DELETE", f"/v1/matches/{match_id}", timeout=900)


@asynccontextmanager
async def configured_provider():
    mode = os.environ.get("ARENA_PROVIDER", "remote")
    if mode == "local":
        from arena.app.provider import LocalArenaProvider
        from arena.app.runtime.executor import Executor
        from arena.app.service import ArenaService
        service = ArenaService(Executor(os.environ.get("ARENA_CASES", "arena/cases")))
        await service.start()
        try:
            yield LocalArenaProvider(service)
        finally:
            await service.close()
    elif mode == "remote":
        url, token = os.environ.get("ARENA_URL"), os.environ.get("ARENA_TOKEN")
        if not url or not token:
            # Portal can serve lobby/history before an Arena is configured.
            yield None
            return
        provider = RemoteArenaProvider(url, token, ca_file=os.environ.get("ARENA_CA_FILE"))
        try:
            yield provider
        finally:
            await provider.close()
    else:
        raise ValueError("ARENA_PROVIDER must be local or remote")
