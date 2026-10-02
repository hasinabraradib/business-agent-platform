import re
from itertools import pairwise

from app.ingestion.chunking import (
    OVERLAP_TOKENS,
    TARGET_TOKENS,
    chunk_document,
    estimate_tokens,
    sentence_spans,
)
from app.ingestion.parsers import parse_csv, parse_markdown, parse_text

SENTENCE = re.compile(r"Sentence \d+ of paragraph \d+ talks about the menu\.")


def _long_paragraph(p: int, sentences: int) -> str:
    return " ".join(
        f"Sentence {i} of paragraph {p} talks about the menu." for i in range(sentences)
    )


def test_chunks_respect_headings() -> None:
    md = "# Menu\n\n## Starters\n\nSamosa is crisp.\n\n## Desserts\n\nMishti doi is sweet.\n"
    chunks = chunk_document(parse_markdown(md))
    assert [(c.metadata.get("section"), c.content) for c in chunks] == [
        ("Menu > Starters", "Samosa is crisp."),
        ("Menu > Desserts", "Mishti doi is sweet."),
    ]


def test_heading_path_resets_at_same_level() -> None:
    md = "# A\n\n## B\n\ntext b\n\n# C\n\ntext c\n"
    assert [c.metadata["section"] for c in chunk_document(parse_markdown(md))] == ["A > B", "C"]


def test_long_sections_are_packed_without_cutting_sentences() -> None:
    text = "\n\n".join(_long_paragraph(p, 30) for p in range(4))
    chunks = chunk_document(parse_text(text))
    assert len(chunks) > 1
    for chunk in chunks:
        # Every chunk is made of whole sentences only.
        remainder = SENTENCE.sub("", chunk.content)
        assert remainder.strip() == "", chunk.content
        assert estimate_tokens(chunk.content) <= TARGET_TOKENS + OVERLAP_TOKENS


def test_neighbouring_chunks_overlap_by_whole_sentences() -> None:
    chunks = chunk_document(parse_text(_long_paragraph(0, 80)))
    assert len(chunks) >= 2
    for prev, nxt in pairwise(chunks):
        first_sentence = SENTENCE.match(nxt.content).group()
        assert first_sentence in prev.content
        overlap = nxt.content[: nxt.content.index(first_sentence) + len(first_sentence)]
        assert estimate_tokens(overlap) <= OVERLAP_TOKENS


def test_all_source_sentences_are_covered_in_order() -> None:
    paragraphs = [_long_paragraph(p, 25) for p in range(3)]
    chunks = chunk_document(parse_text("\n\n".join(paragraphs)))
    seen = []
    for chunk in chunks:
        for sentence in SENTENCE.findall(chunk.content):
            if sentence not in seen:
                seen.append(sentence)
    assert seen == SENTENCE.findall(" ".join(paragraphs))


def test_csv_rows_stay_intact_one_chunk_per_row() -> None:
    csv_text = "dish,price\nKacchi Biryani,450\nBorhani,80\nFirni,120\n"
    chunks = chunk_document(parse_csv(csv_text), source="menu.csv")
    assert [c.content for c in chunks] == [
        "dish: Kacchi Biryani\nprice: 450",
        "dish: Borhani\nprice: 80",
        "dish: Firni\nprice: 120",
    ]
    assert [c.metadata for c in chunks] == [
        {"row": 1, "source": "menu.csv"},
        {"row": 2, "source": "menu.csv"},
        {"row": 3, "source": "menu.csv"},
    ]
    assert [c.index for c in chunks] == [0, 1, 2]


def test_bengali_text_round_trips_unchanged() -> None:
    bengali = (
        "আমাদের রেস্তোরাঁ প্রতিদিন সকাল ১১টা থেকে রাত ১১টা পর্যন্ত খোলা থাকে। "
        "শুক্রবার জুমার নামাজের সময় দুপুর ১টা থেকে ২টা পর্যন্ত বন্ধ থাকে।"
    )
    md = f"## খোলার সময়\n\n{bengali}\n"
    chunks = chunk_document(parse_markdown(md))
    assert [c.content for c in chunks] == [bengali]
    assert chunks[0].metadata["section"] == "খোলার সময়"
    assert chunks[0].content.encode() == bengali.encode()


def test_bengali_sentences_split_on_danda() -> None:
    text = "প্রথম বাক্য। দ্বিতীয় বাক্য! তৃতীয়?"
    assert [text[s:e] for s, e in sentence_spans(text)] == [
        "প্রথম বাক্য।",
        "দ্বিতীয় বাক্য!",
        "তৃতীয়?",
    ]


def test_abbreviations_do_not_end_sentences() -> None:
    text = "Dr. Rahman runs the kitchen. Ask Mr. Karim, e.g. at the desk. Done."
    assert [text[s:e] for s, e in sentence_spans(text)] == [
        "Dr. Rahman runs the kitchen.",
        "Ask Mr. Karim, e.g. at the desk.",
        "Done.",
    ]


def test_embedding_text_prefixes_section_but_content_stays_original() -> None:
    chunk = chunk_document(parse_markdown("## Returns\n\nWithin 7 days."))[0]
    assert chunk.content == "Within 7 days."
    assert chunk.embedding_text() == "Returns\n\nWithin 7 days."


def test_pages_are_recorded() -> None:
    from app.ingestion.parsers import Block, ParsedDocument

    parsed = ParsedDocument(
        blocks=[Block("text", "Page one text.", page=1), Block("text", "Page two.", page=2)]
    )
    assert chunk_document(parsed)[0].metadata == {"page": 1, "page_end": 2}
