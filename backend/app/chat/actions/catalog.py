from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import Select, func, or_

from app.chat.prompts import CITE_REMINDER
from app.chat.tools import Tool, ToolArgs, ToolResult, TurnContext, location
from app.models import CatalogItem, Chunk, Document


def _like(value: str) -> str:
    escaped = value.replace("\\\\", "\\\\\\\\").replace("%", "\\\\%").replace("_", "\\\\_")
    return f"%{escaped}%"


class AttributeFilter(ToolArgs):
    name: str = Field(min_length=1, max_length=40, description="e.g. allergens, spice_level")
    contains: str = Field(min_length=1, max_length=60, description="case-insensitive")


class QueryCatalogArgs(ToolArgs):
    text: str | None = Field(
        default=None,
        max_length=100,
        description="Words to match in the item's name (English or Bengali) or category.",
    )
    category: str | None = Field(default=None, max_length=60, description="e.g. 'Rice', 'Sarees'")
    min_price: float | None = Field(default=None, ge=0)
    max_price: float | None = Field(default=None, ge=0, description="e.g. 500 for 'under 500'")
    in_stock: bool | None = Field(
        default=None, description="true: leave out items marked out of stock"
    )
    attributes: list[AttributeFilter] | None = Field(
        default=None,
        max_length=5,
        description='Attribute filters, e.g. [{"name": "allergens", "contains": "nuts"}] or '
        '[{"name": "material", "contains": "silk"}].',
    )
    sort: Literal["price_asc", "price_desc", "name"] | None = Field(
        default=None, description="price_asc for 'cheapest', price_desc for 'most expensive'"
    )
    limit: int = Field(default=10, ge=1, le=25)

    @model_validator(mode="after")
    def _price_range(self) -> "QueryCatalogArgs":
        low, high = self.min_price, self.max_price
        if low is not None and high is not None and low > high:
            raise ValueError("min_price must not be greater than max_price")
        return self


class QueryCatalogTool(Tool):
    name = "query_catalog"
    args_model = QueryCatalogArgs

    def description(self, settings) -> str:
        return (
            f"Structured lookup over {settings.business_name}'s catalogue (menu or product list): "
            "filter by text, category, price range, stock and attributes; sort and count. "
            "Use for list, filter, count, cheapest and 'under X' questions."
        )

    def guidance(self, settings) -> str:
        return (
            "- Use query_catalog for list, filter, count, cheapest/most expensive and 'under X' "
            "questions about dishes or products (e.g. 'which dishes have nuts?' -> attributes "
            '[{"name": "allergens", "contains": "nuts"}]; "500 takar niche" -> max_price 500). '
            "Use search_knowledge for everything else. Cite catalogue rows by their numbers "
            "[n] like search results."
        )

    def _filtered(self, context: TurnContext, query: Select, args: QueryCatalogArgs) -> Select:
        if args.text:
            pattern = _like(args.text)
            query = query.where(
                or_(
                    CatalogItem.name.ilike(pattern),
                    CatalogItem.alt_name.ilike(pattern),
                    CatalogItem.category.ilike(pattern),
                )
            )
        if args.category:
            query = query.where(CatalogItem.category.ilike(_like(args.category)))
        if args.min_price is not None:
            query = query.where(CatalogItem.price >= args.min_price)
        if args.max_price is not None:
            query = query.where(CatalogItem.price <= args.max_price)
        # A catalogue without a stock column (a menu) has no stock flag: only rows marked out
        # of stock are left out, or in_stock=true would hide the whole menu.
        if args.in_stock is True:
            query = query.where(CatalogItem.in_stock.is_not(False))
        elif args.in_stock is False:
            query = query.where(CatalogItem.in_stock.is_(False))
        for attribute in args.attributes or []:
            key = attribute.name.strip().lower().replace(" ", "_")
            query = query.where(CatalogItem.attributes[key].astext.ilike(_like(attribute.contains)))
        return query

    async def run(self, context: TurnContext, args: QueryCatalogArgs) -> ToolResult:
        async with context.db() as db:
            base = self._filtered(context, db.select(CatalogItem), args)
            total = await db.scalar(base.with_only_columns(func.count()).order_by(None))
            order = {
                "price_asc": (CatalogItem.price.asc().nulls_last(), CatalogItem.name),
                "price_desc": (CatalogItem.price.desc().nulls_last(), CatalogItem.name),
                "name": (CatalogItem.name,),
                None: (CatalogItem.document_id, CatalogItem.row),
            }[args.sort]
            items = await db.scalars(base.order_by(*order).limit(args.limit))
            chunk_ids = [item.chunk_id for item in items if item.chunk_id]
            chunks = {
                c.id: c for c in await db.scalars(db.select(Chunk).where(Chunk.id.in_(chunk_ids)))
            }
            doc_ids = {item.document_id for item in items}
            titles = {
                d.id: d.title
                for d in await db.scalars(db.select(Document).where(Document.id.in_(doc_ids)))
            }
        lines = []
        for item in items:
            chunk = chunks.get(item.chunk_id) if item.chunk_id else None
            marker = ""
            if chunk is not None:
                source = _ChunkView(chunk, titles.get(item.document_id, ""))
                marker = f"[{context.add_source(source)}] "
            name = f"{item.name} ({item.alt_name})" if item.alt_name else item.name
            parts = [name]
            if item.category:
                parts.append(item.category)
            if item.price is not None:
                parts.append(f"{item.currency} {item.price:g}")
            if item.in_stock is not None:
                parts.append("in stock" if item.in_stock else "out of stock")
            attributes = "; ".join(f"{k}: {v}" for k, v in item.attributes.items() if v)
            if attributes:
                parts.append(attributes)
            lines.append(marker + " | ".join(parts))
        if not lines:
            body = "No catalogue items match these filters."
        else:
            body = f"{total} matching items, showing {len(lines)}:\n" + "\n".join(lines)
        content = f"<catalog-results-{context.nonce}>\n{body}\n</catalog-results-{context.nonce}>"
        return ToolResult(
            f"{content}\n{CITE_REMINDER}" if lines else content,
            summary=f"{total} matching, {len(lines)} shown",
        )


class _ChunkView:
    """Adapts a Chunk row to what TurnContext.add_source expects."""

    def __init__(self, chunk: Chunk, title: str) -> None:
        self.chunk_id = chunk.id
        self.document_id = chunk.document_id
        self.document_title = title
        self.metadata = chunk.meta
        self.content = chunk.content


__all__ = ["QueryCatalogArgs", "QueryCatalogTool", "location"]
