"""Catalogue CSVs (menus, product lists) as structured rows in catalog_items.

Column mapping (documents.catalog_mapping): a JSON object mapping catalogue fields to CSV column
names. Any field left out is detected from common column names; every other column becomes an
attribute (keys lowercased, spaces -> underscores):

    {"name": "dish_en", "alt_name": "dish_bn", "category": "category",
     "price": "price_bdt", "currency": "BDT", "in_stock": "stock_status"}

"currency" may be a column name or a literal 3-letter code; by default it comes from a
"currency" column, a price column suffix such as "price_bdt", or the tenant's currency.
"""

import csv
import io
import re
from decimal import Decimal, InvalidOperation
from typing import Any

FIELDS = ("name", "alt_name", "category", "price", "currency", "in_stock")
CANDIDATES = {
    "name": ("name", "name_en", "dish_en", "dish", "item", "product", "title"),
    "alt_name": ("alt_name", "name_bn", "dish_bn", "bn_name", "bangla", "bengali"),
    "category": ("category", "type", "section", "group"),
    "price": ("price", "price_bdt", "price_usd", "cost", "rate"),
    "currency": ("currency",),
    "in_stock": ("in_stock", "stock", "stock_status", "available", "availability"),
}
OUT_OF_STOCK = {"no", "false", "0", "out of stock", "sold out", "unavailable", "not available"}


class CatalogMappingError(ValueError):
    pass


def validate_mapping(mapping: Any) -> dict[str, str]:
    if not isinstance(mapping, dict) or not all(isinstance(v, str) for v in mapping.values()):
        raise CatalogMappingError("catalog_mapping must be a JSON object of field -> column name")
    unknown = set(mapping) - set(FIELDS)
    if unknown:
        raise CatalogMappingError(f"unknown catalogue fields: {sorted(unknown)}; use {FIELDS}")
    return {k: v.strip() for k, v in mapping.items()}


def _key(column: str) -> str:
    return re.sub(r"\s+", "_", column.strip().lower())


def resolve_mapping(header: list[str], mapping: dict[str, str]) -> dict[str, str | None]:
    by_key = {_key(column): column for column in header}
    resolved: dict[str, str | None] = {}
    for field in FIELDS:
        explicit = mapping.get(field)
        if explicit:
            if (
                field == "currency"
                and re.fullmatch(r"[A-Za-z]{3}", explicit)
                and _key(explicit) not in by_key
            ):
                resolved[field] = None  # a literal code, handled by the caller
                continue
            if _key(explicit) not in by_key:
                raise CatalogMappingError(f"column {explicit!r} (for {field}) is not in the CSV")
            resolved[field] = by_key[_key(explicit)]
        else:
            resolved[field] = next((by_key[c] for c in CANDIDATES[field] if c in by_key), None)
    if resolved["name"] is None:
        raise CatalogMappingError("could not find a name column; set catalog_mapping.name")
    return resolved


def parse_price(value: str) -> Decimal | None:
    cleaned = re.sub(r"[^\d.]", "", value.replace(",", ""))
    if not cleaned:
        return None
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def parse_stock(value: str) -> bool | None:
    text = value.strip().lower()
    if not text:
        return None
    return text not in OUT_OF_STOCK


def catalog_rows(text: str, mapping: dict[str, str], default_currency: str) -> list[dict[str, Any]]:
    """One dict per CSV data row (row numbers match the chunker's 'row' metadata)."""
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        return []
    columns = resolve_mapping(header, validate_mapping(mapping))
    literal_currency = mapping.get("currency") if columns["currency"] is None else None
    price_suffix = re.search(r"_([a-z]{3})$", _key(columns["price"] or ""))
    mapped = {c for c in columns.values() if c}
    rows = []
    for number, values in enumerate(reader, start=1):
        record = {h: v.strip() for h, v in zip(header, values, strict=False)}

        def value(field: str, record: dict[str, str] = record) -> str:
            column = columns[field]
            return record.get(column, "") if column else ""

        name = value("name")
        if not name:
            continue  # empty rows are skipped (the chunker skips them too, but keeps counting)
        currency = (
            value("currency")
            or literal_currency
            or (price_suffix.group(1) if price_suffix else None)
            or default_currency
        )
        attributes = {_key(h): v for h, v in record.items() if h not in mapped and v}
        if value("in_stock"):
            attributes["stock_status"] = value("in_stock")
        rows.append(
            {
                "row": number,
                "name": name,
                "alt_name": value("alt_name") or None,
                "category": value("category") or None,
                "price": parse_price(value("price")),
                "currency": currency.upper()[:3],
                "in_stock": parse_stock(value("in_stock")),
                "attributes": attributes,
            }
        )
    return rows
