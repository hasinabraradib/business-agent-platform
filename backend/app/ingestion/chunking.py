"""Structure-aware chunking.

Prose is grouped by section (a heading and everything under it, never crossing into the next
heading), then packed from whole sentences up to about TARGET_TOKENS, preferring paragraph
breaks, with a small overlap of whole sentences between neighbouring chunks of a section.
Chunk content is sliced from the source text, never rewritten. CSV rows become one chunk each.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from app.ingestion.parsers import Block, ParsedDocument

TARGET_TOKENS = 400
OVERLAP_TOKENS = 50
# Prefer to end a chunk at a paragraph break once it is at least this full.
PARAGRAPH_BREAK_FILL = 0.6
# A single "sentence" above this (e.g. a long list with no punctuation) is split at whitespace;
# below it, a long sentence is kept whole even if it exceeds TARGET_TOKENS.
HARD_MAX_TOKENS = 1500


def estimate_tokens(text: str) -> int:
    """Rough token count without a tokenizer download.

    About 4 characters per token for Latin script; Bengali and other non-Latin scripts
    tokenize much more densely, so they are counted at about 2 characters per token.
    """
    ascii_chars = sum(1 for c in text if c.isascii())
    return max(1, round(ascii_chars / 4 + (len(text) - ascii_chars) / 2))


# A sentence ends at . ! ? or the Bengali danda (। ॥), optionally followed by closing quotes or
# brackets, and then whitespace.
SENTENCE_END = re.compile("[.!?\u0964\u0965][\"'\u201d\u2019)\\]]*(?=\\s)")
ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "e.g", "i.e", "no", "approx",
}  # fmt: skip


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) spans of the sentences in text, excluding surrounding whitespace."""
    spans = []
    start = 0
    for match in SENTENCE_END.finditer(text):
        word = re.search(r"([\w.]+)[.!?।॥]*$", text[start : match.start() + 1])
        if word and match.group().startswith("."):
            token = word.group(1).rstrip(".").lower()
            if token in ABBREVIATIONS or (len(token) == 1 and token.isalpha()):
                continue  # "Dr. Rahman", "J. Smith": not a sentence end
        spans.append((start, match.end()))
        start = match.end()
    spans.append((start, len(text)))
    result = []
    for s, e in spans:
        segment = text[s:e]
        if segment.strip():
            lead = len(segment) - len(segment.lstrip())
            result.append((s + lead, s + len(segment.rstrip())))
    return result


@dataclass
class ChunkDraft:
    content: str
    metadata: dict[str, Any]
    index: int = 0

    def embedding_text(self) -> str:
        """The chunk as embedded: section heading, then the original content.

        The document title is passed to the embedding provider alongside this text.
        """
        section = self.metadata.get("section")
        return f"{section}\n\n{self.content}" if section else self.content


@dataclass
class _Sentence:
    paragraph: int  # index into the section's paragraphs
    start: int
    end: int
    tokens: int
    page: int | None


@dataclass
class _Section:
    heading: str | None
    paragraphs: list[Block] = field(default_factory=list)


def _sections(blocks: list[Block]) -> list[_Section]:
    sections = [_Section(heading=None)]
    path: list[tuple[int, str]] = []  # (level, heading) stack
    for block in blocks:
        if block.kind == "heading":
            while path and path[-1][0] >= block.level:
                path.pop()
            path.append((block.level, block.text))
            sections.append(_Section(heading=" > ".join(text for _, text in path)))
        else:
            sections[-1].paragraphs.append(block)
    return [s for s in sections if s.paragraphs]


def _split_oversized(paragraph: int, block: Block, start: int, end: int) -> list[_Sentence]:
    pieces = []
    piece_start = start
    for match in re.finditer(r"\s+", block.text[start:end]):
        cut = start + match.start()
        if estimate_tokens(block.text[piece_start:cut]) >= TARGET_TOKENS:
            pieces.append((piece_start, cut))
            piece_start = start + match.end()
    pieces.append((piece_start, end))
    return [
        _Sentence(paragraph, s, e, estimate_tokens(block.text[s:e]), block.page) for s, e in pieces
    ]


def _sentences(section: _Section) -> list[_Sentence]:
    sentences = []
    for i, block in enumerate(section.paragraphs):
        for start, end in sentence_spans(block.text):
            tokens = estimate_tokens(block.text[start:end])
            if tokens > HARD_MAX_TOKENS:
                sentences.extend(_split_oversized(i, block, start, end))
            else:
                sentences.append(_Sentence(i, start, end, tokens, block.page))
    return sentences


def _render(section: _Section, sentences: list[_Sentence]) -> str:
    """Join sentences: same paragraph -> original text slice; new paragraph -> blank line."""

    def text_of(group: list[_Sentence]) -> str:
        return section.paragraphs[group[0].paragraph].text[group[0].start : group[-1].end]

    parts = []
    group = [sentences[0]]
    for sentence in sentences[1:]:
        if sentence.paragraph == group[-1].paragraph:
            group.append(sentence)
        else:
            parts.append(text_of(group))
            group = [sentence]
    parts.append(text_of(group))
    return "\n\n".join(parts)


def _chunk_section(section: _Section, source: str | None) -> list[ChunkDraft]:
    sentences = _sentences(section)
    groups: list[list[_Sentence]] = []
    current: list[_Sentence] = []
    fresh = 0  # sentences in `current` that are not overlap carried from the previous chunk

    def flush() -> None:
        nonlocal current, fresh
        if fresh == 0:
            return
        groups.append(current)
        overlap: list[_Sentence] = []
        budget = OVERLAP_TOKENS
        for sentence in reversed(current):
            if sentence.tokens > budget or len(overlap) + 1 >= len(current):
                break
            overlap.insert(0, sentence)
            budget -= sentence.tokens
        current, fresh = overlap, 0

    for i, sentence in enumerate(sentences):
        size = sum(s.tokens for s in current)
        new_paragraph = i > 0 and sentence.paragraph != sentences[i - 1].paragraph
        if current and fresh and size + sentence.tokens > TARGET_TOKENS:
            flush()
        elif new_paragraph and fresh and size >= TARGET_TOKENS * PARAGRAPH_BREAK_FILL:
            paragraph_tokens = sum(s.tokens for s in sentences if s.paragraph == sentence.paragraph)
            if size + paragraph_tokens > TARGET_TOKENS:
                flush()
        current.append(sentence)
        fresh += 1
    flush()

    drafts = []
    for group in groups:
        metadata: dict[str, Any] = {}
        if section.heading:
            metadata["section"] = section.heading
        pages = sorted({s.page for s in group if s.page is not None})
        if pages:
            metadata["page"] = pages[0]
            if pages[-1] != pages[0]:
                metadata["page_end"] = pages[-1]
        if source:
            metadata["source"] = source
        drafts.append(ChunkDraft(content=_render(section, group), metadata=metadata))
    return drafts


def chunk_document(parsed: ParsedDocument, source: str | None = None) -> list[ChunkDraft]:
    """Split a parsed document into chunks with metadata (section, page, row, source)."""
    drafts: list[ChunkDraft] = []
    rows = [b for b in parsed.blocks if b.kind == "row"]
    for block in rows:
        metadata: dict[str, Any] = {"row": block.row}
        if source:
            metadata["source"] = source
        drafts.append(ChunkDraft(content=block.text, metadata=metadata))

    prose = [b for b in parsed.blocks if b.kind != "row"]
    for section in _sections(prose):
        drafts.extend(_chunk_section(section, source))

    for index, draft in enumerate(drafts):
        draft.index = index
    return drafts
