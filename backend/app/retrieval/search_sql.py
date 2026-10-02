"""Tenant-scoped vector and keyword search SQL (run through TenantDB.execute_sql)."""

import logging
import re
import uuid
from dataclasses import dataclass

from pgvector.sqlalchemy import Vector
from sqlalchemy import Text, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY

from app.models import EMBEDDING_DIMENSIONS
from app.tenancy import TenantDB

logger = logging.getLogger(__name__)

ITERATIVE_SCAN_MODES = ("off", "strict_order", "relaxed_order")


@dataclass(frozen=True)
class VectorHit:
    chunk_id: uuid.UUID
    similarity: float


@dataclass(frozen=True)
class KeywordHit:
    chunk_id: uuid.UUID
    score: float
    matched_terms: int


# The HNSW index applies the tenant/model filter only after it has produced candidates (about
# hnsw.ef_search of them), so a tenant owning a small share of the table can get fewer than LIMIT
# rows back - even zero. Two layers fix this:
# 1. pgvector 0.8's iterative index scan keeps scanning until LIMIT rows pass the filter (up to
#    hnsw.max_scan_tuples). relaxed_order may return rows slightly out of order, so the
#    materialized CTE is re-sorted by exact distance.
# 2. If that still returns fewer rows than LIMIT while the tenant has more chunks, an exact scan
#    over the tenant's own rows (via the tenant_id index, no HNSW) fills the gap.
VECTOR_SQL = text(
    """
    WITH nearest AS MATERIALIZED (
        SELECT c.id, c.embedding <=> :embedding AS distance
        FROM chunks AS c
        WHERE c.tenant_id = :tenant_id AND c.embedding_model = :model
        ORDER BY c.embedding <=> :embedding
        LIMIT :limit
    )
    SELECT id, 1 - distance AS similarity FROM nearest ORDER BY distance, id
    """
).bindparams(bindparam("embedding", type_=Vector(EMBEDDING_DIMENSIONS)))


# "+ 0" makes the ORDER BY expression unusable for the HNSW index: an exact, tenant-only scan.
EXACT_VECTOR_SQL = text(
    """
    SELECT c.id, 1 - (c.embedding <=> :embedding) AS similarity
    FROM chunks AS c
    WHERE c.tenant_id = :tenant_id AND c.embedding_model = :model
    ORDER BY (c.embedding <=> :embedding) + 0, c.id
    LIMIT :limit
    """
).bindparams(bindparam("embedding", type_=Vector(EMBEDDING_DIMENSIONS)))

COUNT_SQL = text(
    "SELECT count(*) FROM chunks WHERE tenant_id = :tenant_id AND embedding_model = :model"
)


async def vector_search(
    db: TenantDB,
    embedding: list[float],
    model: str,
    limit: int,
    *,
    iterative_scan: str = "relaxed_order",
    ef_search: int = 100,
    exact_fallback: bool = True,
) -> list[VectorHit]:
    if iterative_scan not in ITERATIVE_SCAN_MODES:
        raise ValueError(f"hnsw iterative scan must be one of {ITERATIVE_SCAN_MODES}")
    # Transaction-local, like app.current_tenant.
    await db.set_local(
        {"hnsw.iterative_scan": iterative_scan, "hnsw.ef_search": str(max(ef_search, limit))}
    )
    params = {"embedding": embedding, "model": model, "limit": limit}
    rows = await db.execute_sql(VECTOR_SQL, params)
    hits = [VectorHit(row.id, float(row.similarity)) for row in rows]
    if exact_fallback and len(hits) < limit:
        available = (await db.execute_sql(COUNT_SQL, {"model": model})).scalar_one()
        if available > len(hits):
            logger.info(
                "Vector index returned %d of %d rows (tenant has %d); using an exact scan",
                len(hits),
                limit,
                available,
            )
            rows = await db.execute_sql(EXACT_VECTOR_SQL, params)
            hits = [VectorHit(row.id, float(row.similarity)) for row in rows]
    return hits


# Keyword search over chunks.tsv ('simple' configuration: no stemming, no stop words).
#
# Each query lexeme is its own term, weighted by inverse document frequency within the tenant
# (BM25's idf: ln(1 + (N - df + 0.5) / (df + 0.5))). A chunk's score is the sum of the weights of
# the terms it contains: a single rare term ("borhani", "বোরহানি") is enough to hit, more matched
# terms rank higher, and words found everywhere add almost nothing. On a small tenant corpus idf
# alone cannot tell "do you have" from content words, so a short list of question and function
# words (English, Bengali, romanized Bengali) is dropped first. Compound tokens such as product
# codes are added as
# phrase terms: the parser splits "JL-SAR-001" into jl-sar / jl / sar / 001, so the phrase is
# what makes the exact code outrank its siblings (JL-SAR-002) and other "001" codes.
KEYWORD_SQL = text(
    """
    WITH all_lexemes AS (
        SELECT DISTINCT lexeme
        FROM unnest(tsvector_to_array(to_tsvector('simple', :query))) AS lexeme
    ),
    lexemes AS (
        -- Drop question/function words, unless the query consists of nothing else.
        SELECT lexeme FROM all_lexemes WHERE lexeme <> ALL(:stopwords)
        UNION ALL
        SELECT lexeme FROM all_lexemes
        WHERE NOT EXISTS (SELECT 1 FROM all_lexemes WHERE lexeme <> ALL(:stopwords))
    ),
    terms AS (
        SELECT to_tsquery('simple', quote_literal(lexeme)) AS tsq FROM lexemes
        UNION ALL
        SELECT phraseto_tsquery('simple', phrase) FROM unnest(:phrases) AS phrase
    ),
    corpus AS (
        SELECT count(*)::float8 AS n FROM chunks WHERE tenant_id = :tenant_id
    ),
    any_term AS (
        -- OR of all query lexemes, used only to break ties by term density/proximity.
        SELECT replace(plainto_tsquery('simple', :query)::text, ' & ', ' | ')::tsquery AS q
    ),
    weighted AS (
        SELECT t.tsq,
               ln(1 + (corpus.n - df.df + 0.5) / (df.df + 0.5)) AS idf
        FROM terms AS t
        CROSS JOIN corpus
        CROSS JOIN LATERAL (
            SELECT count(*)::float8 AS df FROM chunks AS c
            WHERE c.tenant_id = :tenant_id AND c.tsv @@ t.tsq
        ) AS df
        WHERE df.df > 0
    )
    SELECT c.id,
           sum(w.idf) AS score,
           count(*) AS matched_terms,
           ts_rank_cd(c.tsv, (SELECT q FROM any_term)) AS density
    FROM chunks AS c
    JOIN weighted AS w ON c.tsv @@ w.tsq
    WHERE c.tenant_id = :tenant_id
    GROUP BY c.id, c.tsv
    ORDER BY score DESC, density DESC, c.id
    LIMIT :limit
    """
).bindparams(bindparam("phrases", type_=ARRAY(Text)), bindparam("stopwords", type_=ARRAY(Text)))

# Tokens joined by - _ . / such as product codes (JL-SAR-001), model numbers or "Mon-Fri".
COMPOUND_TOKEN = re.compile(r"\w+(?:[-_./]\w+)+")


# Question and function words that carry no topic (matched against lowercased lexemes).
# Deliberately short: content words such as "price", "dam" (দাম) or "open" stay searchable.
STOPWORDS = sorted(
    {
        # English
        "a", "an", "the", "and", "or", "but", "if", "of", "to", "in", "on", "at", "by", "for",
        "with", "from", "about", "as", "into", "than", "then", "so", "is", "are", "was", "were",
        "be", "been", "being", "am", "do", "does", "did", "have", "has", "had", "can", "could",
        "will", "would", "shall", "should", "may", "might", "must", "i", "me", "my", "we", "us",
        "our", "you", "your", "it", "its", "they", "them", "their", "he", "she", "this", "that",
        "these", "those", "there", "here", "what", "which", "who", "whom", "whose", "when",
        "where", "why", "how", "any", "some", "all", "not", "no", "yes", "please", "tell",
        "know", "get", "much", "many", "s", "t",
        # Bengali
        "কি", "কী", "কে", "কখন", "কোথায়", "কেন", "কিভাবে", "কীভাবে", "কেমন", "কত", "কোন",
        "আছে", "আছেন", "নেই", "হয়", "হবে", "যায়", "যাবে", "আমি", "আমরা", "আমার", "আমাদের",
        "আপনি", "আপনার", "আপনাদের", "তুমি", "এর", "এ", "ও", "এবং", "বা", "না", "যে",
        "এই", "সেই", "থেকে", "জন্য", "দিয়ে", "সাথে", "টা", "টি",
        # Romanized Bengali ("Banglish")
        "ki", "kokhon", "kothay", "koi", "keno", "kivabe", "kemon", "koto", "kon", "ache",
        "achen", "nai", "nei", "hoy", "hobe", "jay", "jabe", "ami", "amra", "amar", "amader",
        "apni", "apnar", "apnader", "tumi", "er", "ar", "o", "ba", "na", "je", "ei", "sei",
        "theke", "jonno", "diye", "sathe", "ta", "ti", "bhai", "vai",
    }
)  # fmt: skip


def compound_tokens(query: str) -> list[str]:
    return sorted({m.group() for m in COMPOUND_TOKEN.finditer(query)})


async def keyword_search(db: TenantDB, query: str, limit: int) -> list[KeywordHit]:
    rows = await db.execute_sql(
        KEYWORD_SQL,
        {
            "query": query,
            "phrases": compound_tokens(query),
            "stopwords": STOPWORDS,
            "limit": limit,
        },
    )
    return [KeywordHit(row.id, float(row.score), int(row.matched_terms)) for row in rows]
