"""Database-layer tenant isolation: Row-Level Security, checked as the application role."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db_roles import APP_DB_ROLE


async def _insert_tenant(conn: AsyncConnection, slug: str) -> uuid.UUID:
    return await conn.scalar(
        text("INSERT INTO tenants (name, slug) VALUES (:name, :slug) RETURNING id"),
        {"name": slug.title(), "slug": slug},
    )


async def _insert_document(conn: AsyncConnection, tenant_id: uuid.UUID, title: str) -> uuid.UUID:
    return await conn.scalar(
        text(
            "INSERT INTO documents (tenant_id, title, source_type) "
            "VALUES (:t, :title, 'text') RETURNING id"
        ),
        {"t": tenant_id, "title": title},
    )


ZERO_VECTOR = "[" + ",".join(["0"] * 768) + "]"


async def _insert_chunk(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    content: str,
    chunk_index: int = 0,
) -> None:
    await conn.execute(
        text(
            "INSERT INTO chunks (tenant_id, document_id, chunk_index, content, embedding, "
            "embedding_model) VALUES (:t, :d, :i, :c, CAST(:e AS vector), 'test')"
        ),
        {"t": tenant_id, "d": document_id, "i": chunk_index, "c": content, "e": ZERO_VECTOR},
    )


async def _set_tenant(conn: AsyncConnection, tenant_id: uuid.UUID) -> None:
    await conn.execute(
        text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_id)}
    )


@pytest.fixture
async def two_tenants(owner_engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID]:
    async with owner_engine.begin() as conn:
        tenant_a = await _insert_tenant(conn, "tenant-a")
        tenant_b = await _insert_tenant(conn, "tenant-b")
        for title in ("A menu", "A hours"):
            doc_a = await _insert_document(conn, tenant_a, title)
            await _insert_chunk(conn, tenant_a, doc_a, f"{title} chunk")
        doc_b = await _insert_document(conn, tenant_b, "B returns policy")
        await _insert_chunk(conn, tenant_b, doc_b, "B returns chunk")
        await conn.execute(
            text(
                "INSERT INTO api_keys (tenant_id, kind, prefix, key_hash) "
                "VALUES (:a, 'admin', 'aaaaaaaa', :ha), (:b, 'admin', 'bbbbbbbb', :hb)"
            ),
            {"a": tenant_a, "b": tenant_b, "ha": "a" * 64, "hb": "b" * 64},
        )
    return tenant_a, tenant_b


async def test_app_role_cannot_bypass_rls(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            )
        ).one()
    assert row == (APP_DB_ROLE, False, False)


async def test_every_tenant_owned_table_has_forced_rls_and_policy(
    owner_engine: AsyncEngine,
) -> None:
    async with owner_engine.connect() as conn:
        tables = set(
            (
                await conn.scalars(
                    text(
                        "SELECT table_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND column_name = 'tenant_id'"
                    )
                )
            ).all()
        )
        tables.add("tenants")
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                    "  EXISTS (SELECT 1 FROM pg_policies p "
                    "          WHERE p.schemaname = 'public' AND p.tablename = c.relname) "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relname = ANY(:tables)"
                ),
                {"tables": list(tables)},
            )
        ).all()
    assert {"api_keys", "documents", "chunks"} <= tables
    assert {r[0] for r in rows} == tables
    for name, enabled, forced, has_policy in rows:
        assert (enabled, forced, has_policy) == (True, True, True), name


@pytest.mark.parametrize("table", ["documents", "chunks", "api_keys", "tenants"])
async def test_no_tenant_set_sees_zero_rows(app_engine: AsyncEngine, two_tenants, table) -> None:
    async with app_engine.begin() as conn:
        assert await conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_tenant_set_sees_only_its_rows_without_where(
    app_engine: AsyncEngine, two_tenants
) -> None:
    tenant_a, tenant_b = two_tenants
    async with app_engine.begin() as conn:
        await _set_tenant(conn, tenant_a)
        docs = (await conn.execute(text("SELECT tenant_id, title FROM documents"))).all()
        assert sorted(d.title for d in docs) == ["A hours", "A menu"]
        assert {d.tenant_id for d in docs} == {tenant_a}
        assert (await conn.scalars(text("SELECT tenant_id FROM api_keys"))).all() == [tenant_a]
        assert (await conn.scalars(text("SELECT id FROM tenants"))).all() == [tenant_a]
        chunks = (await conn.execute(text("SELECT tenant_id, content FROM chunks"))).all()
        assert sorted(c.content for c in chunks) == ["A hours chunk", "A menu chunk"]

    async with app_engine.begin() as conn:
        await _set_tenant(conn, tenant_b)
        titles = (await conn.scalars(text("SELECT title FROM documents"))).all()
        assert titles == ["B returns policy"]


async def test_tenant_setting_does_not_leak_into_next_transaction(
    app_engine: AsyncEngine, two_tenants
) -> None:
    tenant_a, _ = two_tenants
    async with app_engine.connect() as conn:
        async with conn.begin():
            await _set_tenant(conn, tenant_a)
            assert await conn.scalar(text("SELECT count(*) FROM documents")) == 2
        async with conn.begin():
            assert await conn.scalar(text("SELECT count(*) FROM documents")) == 0


async def test_cannot_write_rows_for_another_tenant(app_engine: AsyncEngine, two_tenants) -> None:
    tenant_a, tenant_b = two_tenants
    with pytest.raises(DBAPIError, match="row-level security"):
        async with app_engine.begin() as conn:
            await _set_tenant(conn, tenant_a)
            await _insert_document(conn, tenant_b, "smuggled")


async def test_cannot_update_rows_of_another_tenant(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, two_tenants
) -> None:
    tenant_a, tenant_b = two_tenants
    async with app_engine.begin() as conn:
        await _set_tenant(conn, tenant_a)
        result = await conn.execute(text("UPDATE documents SET title = 'hacked'"))
        assert result.rowcount == 2
    async with owner_engine.connect() as conn:
        titles = (
            await conn.scalars(
                text("SELECT title FROM documents WHERE tenant_id = :b"), {"b": tenant_b}
            )
        ).all()
    assert titles == ["B returns policy"]


async def test_app_role_cannot_create_tenants(app_engine: AsyncEngine) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_engine.begin() as conn:
            await _insert_tenant(conn, "sneaky")


async def test_resolve_api_key_works_before_tenant_is_known(
    app_engine: AsyncEngine, two_tenants
) -> None:
    tenant_a, _ = two_tenants
    async with app_engine.begin() as conn:
        row = (await conn.execute(text("SELECT * FROM resolve_api_key(:h)"), {"h": "a" * 64})).one()
        assert (row.tenant_id, row.kind) == (tenant_a, "admin")
        assert (await conn.execute(text("SELECT * FROM resolve_api_key('nope')"))).all() == []


async def test_chunk_cannot_reference_another_tenants_document(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, two_tenants
) -> None:
    tenant_a, tenant_b = two_tenants
    async with owner_engine.connect() as conn:
        doc_b = await conn.scalar(
            text("SELECT id FROM documents WHERE tenant_id = :b"), {"b": tenant_b}
        )
    # Passes RLS (tenant_id is A's) but the composite foreign key rejects B's document.
    with pytest.raises(DBAPIError, match="chunks_document_fkey"):
        async with app_engine.begin() as conn:
            await _set_tenant(conn, tenant_a)
            await _insert_chunk(conn, tenant_a, doc_b, "smuggled chunk", chunk_index=99)


async def test_deleting_document_cascades_to_its_chunks_only(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, two_tenants
) -> None:
    tenant_a, _ = two_tenants
    async with app_engine.begin() as conn:
        await _set_tenant(conn, tenant_a)
        await conn.execute(text("DELETE FROM documents WHERE title = 'A menu'"))
    async with owner_engine.connect() as conn:
        remaining = (await conn.scalars(text("SELECT content FROM chunks ORDER BY 1"))).all()
    assert remaining == ["A hours chunk", "B returns chunk"]


async def test_keyword_index_keeps_bengali_words_whole(owner_engine: AsyncEngine) -> None:
    # The 'simple' parser relies on the database's character classification (LC_CTYPE); a C
    # locale would split Bengali words at vowel signs and break keyword search.
    async with owner_engine.connect() as conn:
        lexemes = await conn.scalar(
            text("SELECT tsvector_to_array(to_tsvector('simple', 'কাচ্চি বিরিয়ানি Biryani'))")
        )
    assert sorted(lexemes) == sorted(["biryani", "কাচ্চি", "বিরিয়ানি"])


async def _insert_conversation(conn: AsyncConnection, tenant_id: uuid.UUID) -> uuid.UUID:
    conversation = await conn.scalar(
        text(
            "INSERT INTO conversations (tenant_id, visitor_id) VALUES (:t, 'visitor') RETURNING id"
        ),
        {"t": tenant_id},
    )
    await conn.execute(
        text(
            "INSERT INTO messages (tenant_id, conversation_id, role, content) "
            "VALUES (:t, :c, 'user', 'hello')"
        ),
        {"t": tenant_id, "c": conversation},
    )
    return conversation


async def test_conversations_and_messages_are_tenant_isolated(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, two_tenants
) -> None:
    tenant_a, tenant_b = two_tenants
    async with owner_engine.begin() as conn:
        conv_a = await _insert_conversation(conn, tenant_a)
        conv_b = await _insert_conversation(conn, tenant_b)

    async with app_engine.begin() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM conversations")) == 0
        assert await conn.scalar(text("SELECT count(*) FROM messages")) == 0
    async with app_engine.begin() as conn:
        await _set_tenant(conn, tenant_a)
        assert (await conn.scalars(text("SELECT id FROM conversations"))).all() == [conv_a]
        convs = (await conn.scalars(text("SELECT conversation_id FROM messages"))).all()
        assert convs == [conv_a]
    # A message cannot be attached to another tenant's conversation, even with A's tenant_id.
    with pytest.raises(DBAPIError, match="messages_conversation_fkey"):
        async with app_engine.begin() as conn:
            await _set_tenant(conn, tenant_a)
            await conn.execute(
                text(
                    "INSERT INTO messages (tenant_id, conversation_id, role, content) "
                    "VALUES (:t, :c, 'user', 'smuggled')"
                ),
                {"t": tenant_a, "c": conv_b},
            )
    # Messages are append-only for the application role.
    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_engine.begin() as conn:
            await _set_tenant(conn, tenant_a)
            await conn.execute(text("UPDATE messages SET content = 'edited'"))
