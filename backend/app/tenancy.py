"""Application-layer tenant scoping.

Routes never query tenant-owned tables through a raw session: they use TenantDB, which adds the
tenant filter to every query and stamps tenant_id on every new row. Row-Level Security in the
database is the second layer behind this one.
"""

import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from sqlalchemy import ColumnElement, Result, Select, TextClause, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Tenant, TenantOwned

T = TypeVar("T", bound=TenantOwned)


async def bind_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Set app.current_tenant for the session's current transaction (RLS reads it)."""
    await session.execute(
        text("SELECT set_config('app.current_tenant', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


@asynccontextmanager
async def tenant_db(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> AsyncIterator["TenantDB"]:
    """A TenantDB on a fresh session, for code outside a request (worker, CLI)."""
    async with sessionmaker() as session:
        await bind_tenant(session, tenant_id)
        yield TenantDB(session, tenant_id)


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

    async def get(self, model: type[T], id: uuid.UUID, *, for_update: bool = False) -> T | None:
        statement = self.select(model).where(model.id == id)
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def delete_where(self, model: type[T], *criteria: ColumnElement[bool]) -> int:
        self._require_tenant_owned(model)
        result = await self._session.execute(
            delete(model).where(model.tenant_id == self.tenant_id, *criteria)
        )
        return result.rowcount

    async def delete(self, obj: TenantOwned) -> None:
        self._require_tenant_owned(type(obj))
        if obj.tenant_id != self.tenant_id:
            raise ValueError("Refusing to delete a row that belongs to another tenant")
        await self._session.delete(obj)

    async def count(self, model: type[T], *criteria: ColumnElement[bool]) -> int:
        self._require_tenant_owned(model)
        statement = (
            select(func.count())
            .select_from(model)
            .where(model.tenant_id == self.tenant_id, *criteria)
        )
        return await self._session.scalar(statement) or 0

    async def set_local(self, settings: dict[str, str]) -> None:
        """Set Postgres settings for the current transaction only (e.g. hnsw.ef_search)."""
        for name, value in settings.items():
            await self._session.execute(
                text("SELECT set_config(:name, :value, true)"), {"name": name, "value": value}
            )

    async def execute_sql(self, statement: TextClause, params: dict[str, Any]) -> Result[Any]:
        """Run raw SQL that must filter by :tenant_id; the parameter is filled in here.

        For queries the ORM cannot express (vector and full-text search). Refuses SQL without a
        :tenant_id parameter, so the application-layer filter cannot be forgotten.
        """
        if "tenant_id" not in statement._bindparams:
            raise ValueError("Tenant-scoped SQL must filter on :tenant_id")
        return await self._session.execute(statement, {**params, "tenant_id": self.tenant_id})

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

    def add_all(self, objs: Iterable[TenantOwned]) -> None:
        for obj in objs:
            self.add(obj)

    async def tenant(self) -> Tenant:
        tenant = await self._session.get(Tenant, self.tenant_id)
        if tenant is None:  # pragma: no cover - the key resolved, so the tenant exists
            raise LookupError("Authenticated tenant not found")
        return tenant

    async def flush(self) -> None:
        await self._session.flush()

    async def commit(self) -> None:
        await self._session.commit()
        # app.current_tenant is transaction-local; bind it again for the next transaction.
        await bind_tenant(self._session, self.tenant_id)

    async def rollback(self) -> None:
        await self._session.rollback()
        await bind_tenant(self._session, self.tenant_id)
