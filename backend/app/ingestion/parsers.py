"""Parsers turn a source into structural blocks: headings, prose and table rows.

Block text is kept exactly as it appears in the source (apart from PDF and HTML, whose text must
be extracted), so chunk content can be shown to users verbatim.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Literal

from bs4 import BeautifulSoup, NavigableString, Tag
from pypdf import PdfReader
from pypdf.errors import PyPdfError

NO_EXTRACTABLE_TEXT = "no extractable text; OCR is not supported yet"


class ParseError(Exception):
    """The source could not be turned into text. The message is shown to the tenant."""


@dataclass(frozen=True)
class Block:
    kind: Literal["heading", "text", "row"]
    text: str
    level: int = 0  # headings: 1-6
    page: int | None = None  # PDF: 1-based page number
    row: int | None = None  # CSV: 1-based data row number


@dataclass
class ParsedDocument:
    blocks: list[Block] = field(default_factory=list)
    title: str | None = None  # a title found in the source (HTML <title>), if any


def decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError("file is not valid UTF-8 text") from exc


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n(?:[ \t]*\n)+", text) if p.strip()]


def parse_text(text: str) -> ParsedDocument:
    return ParsedDocument(blocks=[Block("text", p) for p in _paragraphs(text)])


ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
FENCE = re.compile(r"^ {0,3}(```|~~~)")
SETEXT_UNDERLINE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")


def parse_markdown(text: str) -> ParsedDocument:
    """Line-based Markdown structure: ATX/setext headings, paragraphs, fenced code kept whole."""
    blocks: list[Block] = []
    paragraph: list[str] = []
    fence: str | None = None

    def flush() -> None:
        if paragraph and "\n".join(paragraph).strip():
            blocks.append(Block("text", "\n".join(paragraph).strip("\n")))
        paragraph.clear()

    for line in text.splitlines():
        if fence is not None:
            paragraph.append(line)
            if line.lstrip().startswith(fence):
                fence = None
                flush()
            continue
        if match := FENCE.match(line):
            flush()
            fence = match.group(1)
            paragraph.append(line)
        elif match := ATX_HEADING.match(line):
            flush()
            blocks.append(Block("heading", match.group(2).strip(), level=len(match.group(1))))
        elif SETEXT_UNDERLINE.match(line) and len(paragraph) == 1 and paragraph[0].strip():
            level = 1 if line.strip().startswith("=") else 2
            blocks.append(Block("heading", paragraph[0].strip(), level=level))
            paragraph.clear()
        elif not line.strip():
            flush()
        else:
            paragraph.append(line)
    flush()
    return ParsedDocument(blocks=blocks)


def parse_csv(text: str) -> ParsedDocument:
    """One block per data row, rendered as "column: value" lines so a row stays intact."""
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        raise ParseError("CSV file is empty") from None
    if not any(header):
        raise ParseError("CSV file has no header row")

    blocks = []
    for number, row in enumerate(reader, start=1):
        lines = [
            f"{column or f'column {i + 1}'}: {value.strip()}"
            for i, (column, value) in enumerate(zip(header, row, strict=False))
            if value.strip()
        ]
        if lines:
            blocks.append(Block("row", "\n".join(lines), row=number))
    if not blocks:
        raise ParseError("CSV file has no data rows")
    return ParsedDocument(blocks=blocks)


def parse_pdf(data: bytes) -> ParsedDocument:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ParseError("PDF is password-protected")
        pages = [page.extract_text() or "" for page in reader.pages]
    except PyPdfError as exc:
        raise ParseError(f"could not read PDF: {exc}") from exc

    blocks = []
    for number, page_text in enumerate(pages, start=1):
        for paragraph in _paragraphs(page_text):
            # PDF text arrives as visual lines; rejoin them into flowing paragraphs.
            joined = re.sub(r"[ \t]*\n[ \t]*", " ", paragraph).strip()
            if joined:
                blocks.append(Block("text", joined, page=number))
    if not blocks:
        raise ParseError(NO_EXTRACTABLE_TEXT)
    return ParsedDocument(blocks=blocks)


HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
TEXT_TAGS = {"p", "li", "pre", "blockquote", "dt", "dd", "figcaption", "caption", "tr"}
DROP_TAGS = [
    "script", "style", "noscript", "template", "svg", "nav", "header", "footer", "aside",
    "form", "iframe", "button", "select",
]  # fmt: skip
BLOCK_TAGS = HEADING_TAGS | TEXT_TAGS | {"div", "section", "article", "main", "table", "ul", "ol"}


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    # get_text(" ") separates inline elements with spaces; undo that before closing punctuation.
    text = re.sub(r" (?=[.,;:!?।)\]])", "", text)
    return re.sub(r"(?<=[(\[]) ", "", text)


def parse_html(html: str) -> ParsedDocument:
    """Main-text extraction: drop navigation and boilerplate, keep headings and paragraphs."""
    soup = BeautifulSoup(html, "html.parser")
    title = _clean(soup.title.get_text()) if soup.title else None
    for tag in soup(DROP_TAGS):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup

    blocks: list[Block] = []

    def walk(node: Tag) -> None:
        inline: list[str] = []  # loose text and inline elements between block elements

        def flush_inline() -> None:
            if text := _clean(" ".join(inline)):
                blocks.append(Block("text", text))
            inline.clear()

        for child in node.children:
            if type(child) is NavigableString:  # exact type: skips comments, doctypes, CDATA
                inline.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            if child.name not in BLOCK_TAGS and child.find(list(BLOCK_TAGS)) is None:
                inline.append(child.get_text(" "))  # <a>, <strong>, <span>, ...
                continue
            flush_inline()
            if child.name in HEADING_TAGS:
                if text := _clean(child.get_text(" ")):
                    blocks.append(Block("heading", text, level=int(child.name[1])))
            elif child.name == "tr":
                cells = [_clean(c.get_text(" ")) for c in child.find_all(["td", "th"])]
                if any(cells):
                    blocks.append(Block("text", " | ".join(c for c in cells if c)))
            elif child.name in TEXT_TAGS or child.find(list(BLOCK_TAGS)) is None:
                if text := _clean(child.get_text(" ")):
                    blocks.append(Block("text", text))
            else:
                walk(child)
        flush_inline()

    walk(root)
    if not any(b.kind == "text" for b in blocks):
        raise ParseError("no readable text found on the page")
    return ParsedDocument(blocks=blocks, title=title or None)


SOURCE_TYPES = ("pdf", "markdown", "text", "csv")


def parse(source_type: str, data: bytes) -> ParsedDocument:
    """Parse uploaded bytes by source type (pdf, markdown, text, csv)."""
    if source_type == "pdf":
        return parse_pdf(data)
    if source_type == "markdown":
        return parse_markdown(decode_text(data))
    if source_type == "text":
        return parse_text(decode_text(data))
    if source_type == "csv":
        return parse_csv(decode_text(data))
    raise ParseError(f"unsupported source type: {source_type}")
