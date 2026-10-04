import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from shared.arena import CreateMatch
from shared.version import VERSION, API_VERSION


bearer = HTTPBearer(auto_error=False)


def authenticate(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if credentials is None or not secrets.compare_digest(credentials.credentials, request.app.state.token):
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})


router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])


@router.get('/info')
async def info(request: Request):
    return {'api_version': API_VERSION, 'version': VERSION, 'ready': request.app.state.service.ready}


def status_json(status):
    result = status.model_dump(mode="json")
    if status.blue_password is not None:
        result["blue_password"] = status.blue_password.get_secret_value()
    return result


@router.post("/matches", status_code=202)
async def create(body: CreateMatch, request: Request):
    return status_json(await request.app.state.service.create_match(body))


@router.get("/matches/{match_id}")
async def get(match_id: int, request: Request):
    if match_id <= 0:
        raise HTTPException(422, "Invalid match ID")
    return status_json(await request.app.state.service.get_match(match_id))


@router.get("/matches/{match_id}/events")
async def events(match_id: int, request: Request, after: int = Query(0, ge=0)):
    return await request.app.state.service.get_events(match_id, after)


@router.delete("/matches/{match_id}", status_code=204)
async def destroy(match_id: int, request: Request):
    if match_id <= 0:
        raise HTTPException(422, "Invalid match ID")
    await request.app.state.service.destroy_match(match_id)
    return Response(status_code=204)
