import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response, status

from app.auth import AdminAuth
from app.models import ApiKey
from app.schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut
from app.security import new_api_key

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


@router.post("", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_api_key(body: ApiKeyCreate, auth: AdminAuth):
    created = new_api_key(auth.tenant_id, body.kind, body.label)
    auth.db.add(created.record)
    await auth.db.flush()
    response = ApiKeyCreated(
        **ApiKeyOut.model_validate(created.record).model_dump(), key=created.full_key
    )
    await auth.db.commit()
    return response


@router.get("", response_model=list[ApiKeyOut])
async def list_api_keys(auth: AdminAuth):
    return await auth.db.scalars(auth.db.select(ApiKey).order_by(ApiKey.created_at.desc()))


@router.delete("/{api_key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(api_key_id: uuid.UUID, auth: AdminAuth) -> Response:
    key = await auth.db.get(ApiKey, api_key_id)
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    if key.revoked_at is None:
        if key.kind == "admin":
            other_active_admins = await auth.db.count(
                ApiKey, ApiKey.kind == "admin", ApiKey.revoked_at.is_(None), ApiKey.id != key.id
            )
            if not other_active_admins:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot revoke the tenant's last active admin key",
                )
        key.revoked_at = datetime.now(UTC)
        await auth.db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
