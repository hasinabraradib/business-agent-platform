"""The labelled question sets in evals/data/ and the chunk keys they refer to."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
EVALS_DIR = REPO_ROOT / "evals"
DATA_DIR = EVALS_DIR / "data"
RESULTS_DIR = EVALS_DIR / "results"
CONFIG_PATH = EVALS_DIR / "config.json"

# Eval tenant name -> (demo tenant slug, question file)
TENANTS = {
    "restaurant": ("demo-restaurant", "restaurant.jsonl"),
    "shop": ("demo-shop", "shop.jsonl"),
}
LANGUAGES = ("en", "banglish", "bn")
STYLES = ("plain", "typo")


@dataclass(frozen=True)
class Question:
    id: str
    tenant: str
    question: str
    language: str
    style: str
    answerable: bool
    expected: tuple[str, ...]  # chunk keys; any one of them answers the question


class DatasetError(ValueError):
    pass


def chunk_key(metadata: dict[str, Any]) -> str:
    """'about.md#Opening hours' (the last heading of the section) or 'menu.csv#row3'.
    Stable across re-ingestion, unlike chunk ids."""
    source = metadata.get("source", "?")
    if "row" in metadata:
        return f"{source}#row{metadata['row']}"
    section = str(metadata.get("section", ""))
    return f"{source}#{section.split(' > ')[-1]}"


def load_questions(tenant: str, data_dir: Path = DATA_DIR) -> list[Question]:
    _, filename = TENANTS[tenant]
    questions = []
    for number, line in enumerate(
        (data_dir / filename).read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise DatasetError(f"{filename}:{number}: not JSON") from exc
        questions.append(
            Question(
                id=row["id"],
                tenant=tenant,
                question=row["question"],
                language=row["language"],
                style=row.get("style", "plain"),
                answerable=bool(row["answerable"]),
                expected=tuple(row.get("expected", [])),
            )
        )
    validate(questions)
    return questions


def validate(questions: list[Question], known_keys: set[str] | None = None) -> None:
    """Honest labels: unique ids, known languages, answerable <=> has an expected chunk, and
    (when the ingested chunk keys are known) every expected key exists."""
    seen: set[str] = set()
    for q in questions:
        if q.id in seen:
            raise DatasetError(f"duplicate id {q.id}")
        seen.add(q.id)
        if q.language not in LANGUAGES or q.style not in STYLES:
            raise DatasetError(f"{q.id}: unknown language or style")
        if q.answerable != bool(q.expected):
            raise DatasetError(f"{q.id}: answerable questions need expected chunks, others none")
        if known_keys is not None:
            missing = [key for key in q.expected if key not in known_keys]
            if missing:
                raise DatasetError(f"{q.id}: expected chunks not in the index: {missing}")
