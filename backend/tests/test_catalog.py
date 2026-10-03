"""Catalogue CSVs stored as catalog_items, and the query_catalog tool."""

import json
import re

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.chat.actions.catalog import QueryCatalogArgs, QueryCatalogTool
from app.chat.prompts import CITE_REMINDER, system_prompt
from app.chat.settings import TenantChatSettings
from app.embeddings import FakeEmbeddingProvider
from app.ingestion.pipeline import IngestDeps, process_document
from tests.action_helpers import (
    MENU_CSV,
    NOW,
    PRODUCTS_CSV,
    call,
    make_context,
    registry,
    restaurant_settings,
    set_settings,
)
from tests.conftest import bearer


async def upload_catalog(client, key, name, csv_text, deps, job_queue, **form):
    response = await client.post(
        "/v1/documents",
        headers=bearer(key),
        files={"file": (name, csv_text.encode())},
        data={"catalog": "true", **form},
    )
    assert response.status_code == 202, response.text
    for tenant_id, document_id in job_queue.jobs:
        assert await process_document(deps, tenant_id, document_id) == "ready"
    job_queue.jobs.clear()
    return response.json()["id"]


@pytest.fixture
def deps(app_engine, storage) -> IngestDeps:
    return IngestDeps(
        sessionmaker=async_sessionmaker(app_engine, expire_on_commit=False),
        embedder=FakeEmbeddingProvider(),
        storage=storage,
    )


@pytest.fixture
async def menu(client, make_tenant, owner_engine, deps, job_queue):
    tenant = await make_tenant("catalog-cafe")
    await set_settings(owner_engine, tenant.id, restaurant_settings())
    document_id = await upload_catalog(
        client, tenant.admin_key, "menu.csv", MENU_CSV, deps, job_queue
    )
    return tenant, document_id


# --- ingestion ---------------------------------------------------------------------------------


async def test_catalogue_csv_rows_are_stored_with_their_chunks(menu, owner_engine) -> None:
    _, document_id = menu
    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT i.row, i.name, i.alt_name, i.category, i.price, i.currency, "
                    "i.attributes, c.metadata->>'row' AS chunk_row FROM catalog_items i "
                    "JOIN chunks c ON c.id = i.chunk_id WHERE i.document_id = :d ORDER BY i.row"
                ),
                {"d": document_id},
            )
        ).all()
    assert [r.name for r in rows] == [
        "Kacchi Biryani",
        "Morog Polao",
        "Begun Bharta",
        "Borhani",
        "Firni",
    ]
    first = rows[0]
    assert (first.alt_name, first.category, float(first.price), first.currency) == (
        "কাচ্চি বিরিয়ানি", "Rice", 480.0, "BDT"
    )  # fmt: skip
    assert first.attributes == {"spice_level": "Medium", "allergens": "Dairy, Nuts"}
    assert all(int(r.chunk_row) == r.row for r in rows)  # each item cites its own row's chunk


async def test_reingest_replaces_catalogue_rows(
    client, menu, owner_engine, job_queue, deps
) -> None:
    tenant, document_id = menu

    async def ids():
        async with owner_engine.connect() as conn:
            return set((await conn.scalars(
                text("SELECT id FROM catalog_items WHERE document_id = :d"), {"d": document_id}
            )).all())  # fmt: skip

    before = await ids()
    await client.post(f"/v1/documents/{document_id}/reingest", headers=bearer(tenant.admin_key))
    for tenant_id, doc in job_queue.jobs:
        await process_document(deps, tenant_id, doc)
    after = await ids()
    assert len(after) == len(before) == 5 and not after & before


async def test_explicit_mapping_and_stock_parsing(
    client, make_tenant, deps, job_queue, owner_engine
):
    tenant = await make_tenant("catalog-shop")
    mapping = '{"name": "name", "in_stock": "stock_status"}'
    await upload_catalog(client, tenant.admin_key, "p.csv", PRODUCTS_CSV, deps, job_queue,
                         catalog_mapping=mapping)  # fmt: skip
    async with owner_engine.connect() as conn:
        stock = dict((await conn.execute(text("SELECT name, in_stock FROM catalog_items"))).all())
    assert stock == {
        "Dhakai Jamdani Saree": True,
        "Tangail Taant Saree": True,  # low stock is still in stock
        "Rajshahi Silk Saree": False,
        "Jute Tote Bag": True,
    }


@pytest.mark.parametrize(
    ("name", "form", "detail"),
    [
        ("menu.csv", {"catalog_mapping": '{"price": "no_such_column"}'}, None),
        ("menu.csv", {"catalog_mapping": '{"colour": "x"}'}, "unknown catalogue fields"),
        ("menu.csv", {"catalog_mapping": "not json"}, "not valid JSON"),
        ("menu.txt", {}, "Only CSV files"),
    ],
)
async def test_bad_catalogue_uploads(client, make_tenant, name, form, detail, deps, job_queue):
    tenant = await make_tenant("catalog-bad")
    response = await client.post(
        "/v1/documents", headers=bearer(tenant.admin_key),
        files={"file": (name, MENU_CSV.encode())}, data={"catalog": "true", **form},
    )  # fmt: skip
    if detail is None:  # an unknown column is only found while ingesting: the document fails
        assert response.status_code == 202
        tenant_id, document_id = job_queue.jobs[-1]
        assert await process_document(deps, tenant_id, document_id) == "failed"
        failed = await client.get(f"/v1/documents/{document_id}", headers=bearer(tenant.admin_key))
        assert "no_such_column" in failed.json()["error"]
    else:
        assert response.status_code == 422 and detail in response.text


# --- query_catalog -----------------------------------------------------------------------------


async def query(app_engine, tenant, **arguments):
    context = make_context(app_engine, tenant.id, restaurant_settings())
    content, record = await call(context, "query_catalog", arguments)
    return content, record, context


def names(content: str) -> list[str]:
    import re

    return [m.split(" (")[0] for m in re.findall(r"^\[\d+\] ([^|\n]+?)(?: \||$)", content, re.M)]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"text": "biryani"}, ["Kacchi Biryani"]),
        ({"text": "বোরহানি"}, ["Borhani"]),  # Bengali name
        ({"category": "rice"}, ["Kacchi Biryani", "Morog Polao"]),
        ({"max_price": 140}, ["Begun Bharta", "Borhani", "Firni"]),
        ({"min_price": 300, "max_price": 400}, ["Morog Polao"]),
        ({"attributes": [{"name": "allergens", "contains": "nuts"}]},
         ["Kacchi Biryani", "Morog Polao", "Firni"]),
        ({"attributes": [{"name": "Spice Level", "contains": "medium"}]},
         ["Kacchi Biryani", "Begun Bharta"]),
        ({"attributes": [{"name": "allergens", "contains": "nuts"}], "max_price": 200}, ["Firni"]),
        ({"category": "Rice", "sort": "price_asc", "limit": 1}, ["Morog Polao"]),
        ({"sort": "price_desc", "limit": 2}, ["Kacchi Biryani", "Morog Polao"]),
    ],
)  # fmt: skip
async def test_query_catalog_filters(app_engine, menu, arguments, expected) -> None:
    tenant, _ = menu
    content, record, _ = await query(app_engine, tenant, **arguments)
    assert names(content) == expected
    assert record.status == "ok"


async def test_query_catalog_counts_and_cites_rows(app_engine, menu) -> None:
    tenant, _ = menu
    content, record, context = await query(app_engine, tenant, max_price=500, limit=2)
    assert content.startswith("<catalog-results-n0nce123>")
    assert "5 matching items, showing 2" in content
    assert record.result_summary == "5 matching, 2 shown"
    assert set(context.valid_markers) == {1, 2}  # citable like search results
    assert context.sources[1].metadata["row"] == 1
    assert "BDT 480" in content and "allergens: Dairy, Nuts" in content
    assert content.endswith(f"</catalog-results-n0nce123>\n{CITE_REMINDER}")


async def test_query_catalog_in_stock_filter(
    app_engine, client, make_tenant, deps, job_queue
) -> None:
    tenant = await make_tenant("catalog-stock")
    await upload_catalog(client, tenant.admin_key, "p.csv", PRODUCTS_CSV, deps, job_queue)
    content, _, _ = await query(app_engine, tenant, category="sarees", in_stock=True)
    assert names(content) == ["Dhakai Jamdani Saree", "Tangail Taant Saree"]
    under, _, _ = await query(app_engine, tenant, text="saree", max_price=5000)
    assert names(under) == ["Tangail Taant Saree"]


async def test_in_stock_filter_keeps_rows_without_a_stock_column(app_engine, menu) -> None:
    # The real model sent in_stock=true for "500 takar niche ki ki ache?"; the menu has no
    # stock column, so every row was hidden and the reply said nothing was under 500.
    tenant, _ = menu
    filtered, _, _ = await query(app_engine, tenant, max_price=500, in_stock=True)
    unfiltered, _, _ = await query(app_engine, tenant, max_price=500)
    assert names(filtered) == names(unfiltered) != []
    out_of_stock, _, _ = await query(app_engine, tenant, in_stock=False)
    assert names(out_of_stock) == []  # unknown stock is not "out of stock" either


async def test_query_catalog_empty_result(app_engine, menu) -> None:
    tenant, _ = menu
    content, record, context = await query(app_engine, tenant, text="pizza")
    assert "No catalogue items match" in content
    assert record.result_summary == "0 matching, 0 shown" and context.valid_markers == set()


@pytest.mark.parametrize(
    "arguments",
    [
        {"min_price": 500, "max_price": 100},
        {"limit": 500},
        {"colour": "red"},
        {"max_price": "cheap"},
    ],
)
async def test_invalid_catalogue_arguments_go_back_to_the_model(
    app_engine, menu, arguments
) -> None:
    tenant, _ = menu
    content, record, _ = await query(app_engine, tenant, **arguments)
    assert record.status == "invalid_arguments"
    assert (
        content.startswith("Invalid arguments for query_catalog") and "Nothing was done" in content
    )


def test_list_questions_are_routed_to_query_catalog() -> None:
    settings = TenantChatSettings.from_tenant("Cafe", restaurant_settings())
    prompt = system_prompt(settings, NOW, "n", registry().guidance(settings))
    assert "Use query_catalog for list, filter, count, cheapest" in prompt
    assert "Use search_knowledge for everything else" in prompt
    spec = next(s for s in registry().specs(settings) if s.name == "query_catalog")
    assert "list, filter, count, cheapest and 'under X' questions" in spec.description


async def test_offline_model_routes_list_questions_to_the_catalogue(
    app, client, app_engine, menu
) -> None:
    from app.chat.deps import get_chat_service
    from app.chat.service import ChatService
    from app.llm import Candidate, ChatChain, FakeChatProvider

    tenant, _ = menu
    service = ChatService(
        async_sessionmaker(app_engine, expire_on_commit=False),
        ChatChain([Candidate(FakeChatProvider(), "fake-chat")]),
        registry(),
        clock=lambda: NOW,
    )
    app.dependency_overrides[get_chat_service] = lambda: service
    body = (
        await client.post(
            "/v1/chat",
            json={"visitor_id": "v", "message": "Which dishes have nuts?", "stream": False},
            headers=bearer(tenant.admin_key),
        )
    ).json()
    app.dependency_overrides.pop(get_chat_service)
    assert [t["tool"] for t in body["retrieval"]["tools"]] == ["query_catalog"]
    assert body["outcome"] == "answered"
    assert body["reply"] == ("Here's what we have: Kacchi Biryani [1], Morog Polao [2], Firni [3]")
    assert [c["marker"] for c in body["citations"]] == [1, 2, 3]


def test_guidance_example_matches_the_argument_schema() -> None:
    # The guidance once showed attributes as {"allergens": "nuts"}, which the schema rejects.
    guidance = QueryCatalogTool().guidance(None)
    example = json.loads(re.search(r"attributes (\[.*?\])", guidance).group(1))
    args = QueryCatalogArgs(attributes=example)
    assert args.attributes[0].name == "allergens"
