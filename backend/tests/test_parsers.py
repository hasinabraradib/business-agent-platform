import pytest

from app.ingestion.parsers import (
    NO_EXTRACTABLE_TEXT,
    ParseError,
    parse,
    parse_csv,
    parse_html,
    parse_markdown,
    parse_text,
)
from tests.pdf_factory import scanned_pdf, text_pdf


def test_plain_text_splits_paragraphs_and_keeps_text_verbatim() -> None:
    text = "First paragraph,\nstill first.\n\n  \nSecond paragraph.\n"
    blocks = parse_text(text).blocks
    assert [b.text for b in blocks] == ["First paragraph,\nstill first.", "Second paragraph."]
    assert {b.kind for b in blocks} == {"text"}


def test_markdown_headings_paragraphs_and_code_fences() -> None:
    md = (
        "# Rosa's Kitchen\n\nIntro line.\n\n## Opening hours\nMon-Fri 10-22.\n"
        "Weekends 12-23.\n\nSetext heading\n--------------\n\n"
        "```\n# not a heading\n\nstill code\n```\n"
    )
    blocks = parse_markdown(md).blocks
    assert [(b.kind, b.level, b.text) for b in blocks] == [
        ("heading", 1, "Rosa's Kitchen"),
        ("text", 0, "Intro line."),
        ("heading", 2, "Opening hours"),
        ("text", 0, "Mon-Fri 10-22.\nWeekends 12-23."),
        ("heading", 2, "Setext heading"),
        ("text", 0, "```\n# not a heading\n\nstill code\n```"),
    ]


def test_csv_rows_render_as_column_value_lines() -> None:
    csv_text = (
        "name,name_bn,price_bdt,allergens\n"
        'Kacchi Biryani,কাচ্চি বিরিয়ানি,450,"nuts, dairy"\n'
        "Plain Rice,সাদা ভাত,60,\n"
        "\n"
    )
    blocks = parse_csv(csv_text).blocks
    assert [b.row for b in blocks] == [1, 2]
    assert blocks[0].text == (
        "name: Kacchi Biryani\nname_bn: কাচ্চি বিরিয়ানি\nprice_bdt: 450\nallergens: nuts, dairy"
    )
    assert blocks[1].text == "name: Plain Rice\nname_bn: সাদা ভাত\nprice_bdt: 60"  # empty skipped


def test_csv_semicolon_delimiter_is_detected() -> None:
    blocks = parse_csv("sku;title\nA-1;Tote bag\n").blocks
    assert blocks[0].text == "sku: A-1\ntitle: Tote bag"


@pytest.mark.parametrize("bad", ["", "\n\n", "only,header\n"])
def test_csv_without_data_rows_fails(bad: str) -> None:
    with pytest.raises(ParseError):
        parse_csv(bad)


def test_pdf_text_is_extracted_with_page_numbers() -> None:
    pdf = text_pdf([["Shipping takes 3 days.", "Inside Dhaka only."], ["Returns within 7 days."]])
    blocks = parse("pdf", pdf).blocks
    assert [(b.page, b.text) for b in blocks] == [
        (1, "Shipping takes 3 days. Inside Dhaka only."),
        (2, "Returns within 7 days."),
    ]


def test_scanned_pdf_fails_with_no_ocr_message() -> None:
    with pytest.raises(ParseError) as exc_info:
        parse("pdf", scanned_pdf())
    assert str(exc_info.value) == NO_EXTRACTABLE_TEXT
    assert NO_EXTRACTABLE_TEXT == "no extractable text; OCR is not supported yet"


def test_corrupt_pdf_fails_cleanly() -> None:
    with pytest.raises(ParseError, match="could not read PDF"):
        parse("pdf", b"%PDF-1.4\nthis is not really a pdf")


def test_non_utf8_text_is_rejected() -> None:
    with pytest.raises(ParseError, match="UTF-8"):
        parse("text", "café".encode("latin-1"))


def test_utf8_bom_is_ignored() -> None:
    assert parse("csv", "﻿name\nTea\n".encode()).blocks[0].text == "name: Tea"


def test_html_keeps_main_text_and_drops_boilerplate() -> None:
    html = """<html><head><title>Hours | Rosa's</title><style>p{}</style></head><body>
      <nav><a href="/">Home</a> <a href="/menu">Menu</a></nav>
      <main><h1>Opening hours</h1><p>We open <b>daily</b> at 10am.</p>
      <ul><li>Lunch buffet</li><li>Dinner</li></ul><script>track()</script></main>
      <footer>© 2026</footer></body></html>"""
    parsed = parse_html(html)
    assert parsed.title == "Hours | Rosa's"
    assert [(b.kind, b.text) for b in parsed.blocks] == [
        ("heading", "Opening hours"),
        ("text", "We open daily at 10am."),
        ("text", "Lunch buffet"),
        ("text", "Dinner"),
    ]


def test_html_without_text_fails() -> None:
    with pytest.raises(ParseError, match="no readable text"):
        parse_html("<html><body><script>x()</script><nav>Home</nav></body></html>")
