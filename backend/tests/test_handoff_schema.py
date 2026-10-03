"""Schema for handoff and Telegram: encrypted secrets and isolation of the new tables."""

import uuid

import pytest
from sqlalchemy import text

from app import secret_box

NEW_TABLES = ("telegram_channels", "telegram_updates", "staff_alerts")


def test_secret_box_round_trip_and_tenant_binding() -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    sealed = secret_box.encrypt("123456:ABC-bot-token", tenant)
    assert sealed.startswith("v1:") and "ABC-bot-token" not in sealed
    assert secret_box.decrypt(sealed, tenant) == "123456:ABC-bot-token"
    assert secret_box.encrypt("same", tenant) != secret_box.encrypt("same", tenant)  # nonce
    with pytest.raises(secret_box.SecretBoxError):
        secret_box.decrypt(sealed, other)  # copied into another tenant's row: useless
    with pytest.raises(secret_box.SecretBoxError):
        secret_box.decrypt(sealed[:-4] + "AAAA", tenant)  # tampered


def test_secret_box_refuses_to_work_without_a_key(monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "secrets_encryption_key", "")
    with pytest.raises(secret_box.SecretBoxError, match="not set"):
        secret_box.encrypt("token", uuid.uuid4())


async def test_new_tables_are_isolated_at_the_database(owner_engine, app_engine, make_tenant):
    a, b = await make_tenant("schema-a"), await make_tenant("schema-b")
    async with owner_engine.begin() as conn:
        for tenant, n in ((a, 1), (b, 2)):
            conversation = await conn.scalar(
                text(
                    "INSERT INTO conversations (tenant_id, visitor_id, channel) "
                    "VALUES (:t, :v, 'telegram') RETURNING id"
                ),
                {"t": tenant.id, "v": f"tg:{n}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO telegram_channels (tenant_id, bot_token_encrypted) "
                    "VALUES (:t, 'v1:x')"
                ),
                {"t": tenant.id},
            )
            await conn.execute(
                text("INSERT INTO telegram_updates (tenant_id, update_id) VALUES (:t, 7)"),
                {"t": tenant.id},
            )
            await conn.execute(
                text(
                    "INSERT INTO staff_alerts (tenant_id, conversation_id, chat_id, "
                    "telegram_message_id) VALUES (:t, :c, -100, 5)"
                ),
                {"t": tenant.id, "c": conversation},
            )
    async with app_engine.begin() as conn:
        for table in NEW_TABLES:
            assert await conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(a.id)}
        )
        for table in NEW_TABLES:
            tenants = (await conn.scalars(text(f"SELECT DISTINCT tenant_id FROM {table}"))).all()
            assert tenants == [a.id], table


async def test_conversation_statuses_and_staff_role_are_enforced(owner_engine, make_tenant):
    tenant = await make_tenant("schema-status")
    async with owner_engine.begin() as conn:
        conversation = await conn.scalar(
            text("INSERT INTO conversations (tenant_id, visitor_id) VALUES (:t, 'v') RETURNING id"),
            {"t": tenant.id},
        )
        assert (
            await conn.scalar(
                text("SELECT status FROM conversations WHERE id = :c"), {"c": conversation}
            )
            == "ai"
        )
        await conn.execute(
            text(
                "INSERT INTO messages (tenant_id, conversation_id, role, content) "
                "VALUES (:t, :c, 'staff', 'Hi, this is Rumana from the team')"
            ),
            {"t": tenant.id, "c": conversation},
        )
    with pytest.raises(Exception, match="conversations_status_check"):
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("UPDATE conversations SET status = 'open' WHERE id = :c"), {"c": conversation}
            )
