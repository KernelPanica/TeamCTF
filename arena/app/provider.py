from shared.arena import ArenaProvider


class LocalArenaProvider(ArenaProvider):
    def __init__(self, service):
        self.service = service

    async def create_match(self, request):
        return await self.service.create_match(request)

    async def get_match(self, match_id):
        return await self.service.get_match(match_id)

    async def get_events(self, match_id, after=0):
        return await self.service.get_events(match_id, after)

    async def destroy_match(self, match_id):
        await self.service.destroy_match(match_id)
