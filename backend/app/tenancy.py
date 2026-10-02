"""Application-layer tenant scoping.

Routes never query tenant-owned tables through a raw session: they use TenantDB, which adds the
tenant filter to every query and stamps tenant_id on every new row. Row-Level Security in the
database is the second layer behind this one.
"""

import uuid
from typing import Any, TypeVar

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Tenant, TenantOwned

T = TypeVar("T", bound=TenantOwned)


class TenantDB:
    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self.tenant_id = tenant_id

    @staticmethod
    def _require_tenant_owned(model: type[Any]) -> None:
        if not (isinstance(model, type) and issubclass(model, TenantOwned)):
            raise TypeError(f"{model!r} is not a TenantOwned model")

    def select(self, model: type[T]) -> Select[tuple[T]]:
        """SELECT on a tenant-owned model, already filtered to the current tenant."""
        self._require_tenant_owned(model)
        return select(model).where(model.tenant_id == self.tenant_id)

    async def get(self, model: type[T], id: uuid.UUID) -> T | None:
        return await self._session.scalar(self.select(model).where(model.id == id))

    async def count(self, model: type[T], *criteria: ColumnElement[bool]) -> int:
        self._require_tenant_owned(model)
        statement = (
            select(func.count())
            .select_from(model)
            .where(model.tenant_id == self.tenant_id, *criteria)
        )
        return await self._session.scalar(statement) or 0

    async def scalars(self, statement: Select[tuple[T]]) -> list[T]:
        return list((await self._session.scalars(statement)).all())

    async def scalar(self, statement: Select[Any]) -> Any:
        return await self._session.scalar(statement)

    def add(self, obj: TenantOwned) -> None:
        self._require_tenant_owned(type(obj))
        if obj.tenant_id is None:
            obj.tenant_id = self.tenant_id
        elif obj.tenant_id != self.tenant_id:
            raise ValueError("Refusing to add a row that belongs to another tenant")
        self._session.add(obj)

    async def tenant(self) -> Tenant:
        tenant = await self._session.get(Tenant, self.tenant_id)
        if tenant is None:  # pragma: no cover - the key resolved, so the tenant exists
            raise LookupError("Authenticated tenant not found")
        return tenant

    async def flush(self) -> None:
        await self._session.flush()

    async def commit(self) -> None:
        # The tenant setting is transaction-local: queries after commit see no tenant rows.
        await self._session.commit()
