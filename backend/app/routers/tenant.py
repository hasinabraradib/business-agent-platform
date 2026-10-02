from fastapi import APIRouter

from app.auth import AdminAuth
from app.schemas import TenantOut

router = APIRouter(tags=["tenant"])


@router.get("/tenant", response_model=TenantOut)
async def get_tenant(auth: AdminAuth):
    return await auth.db.tenant()
