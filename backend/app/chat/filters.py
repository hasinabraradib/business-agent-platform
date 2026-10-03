"""Streaming filters applied to the model's output before the customer sees it."""

import re

from app.chat.prompts import OUTCOME_TAGS

TAG = re.compile(r"^\s*\[\[(" + "|".join(OUTCOME_TAGS) + r")\]\][ \t]*\n?")
ANY_TAG = re.compile(r"\[\[(" + "|".join(OUTCOME_TAGS) + r")\]\]")
TAG_TEXTS = tuple(f"[[{tag}]]" for tag in OUTCOME_TAGS)


class OutcomeTagFilter:
    """Strips the leading [[status]] line, buffering only until it can be recognised.

    Some models (gpt-oss) write the tag at the end instead, and then keep going: a status tag
    after the reply has started ends the reply, and everything after it is dropped. Text that
    could be the start of a tag is held back until it resolves; "[[1]]" passes through.
    """

    MAX_TAG_PREFIX = 24

    def __init__(self) -> None:
        self.tag: str | None = None
        self._buffer = ""
        self._done = False  # past the leading-tag check
        self._ended = False  # a later tag ended the reply
        self._held = ""  # a possible tag prefix at the end of the text so far

    def feed(self, text: str) -> str:
        if self._done:
            return self._scan(text)
        self._buffer += text
        stripped = self._buffer.lstrip()
        match = TAG.match(self._buffer)
        if match and (match.group(0).endswith("\n") or len(self._buffer) > match.end()):
            self.tag = match.group(1)
            return self._release(self._buffer[match.end() :].lstrip("\n"))
        could_be_tag = (
            "[[".startswith(stripped[:2])
            and len(stripped) <= self.MAX_TAG_PREFIX
            and "\n" not in stripped
        )
        if stripped and not could_be_tag:
            return self._release(self._buffer)  # no tag: pass everything through
        return ""

    def flush(self) -> str:
        if self._done:
            held, self._held = self._held, ""
            return "" if self._ended else held
        match = TAG.match(self._buffer)
        if match:
            self.tag = match.group(1)
            return self._release(self._buffer[match.end() :]) + self.flush()
        return self._release(self._buffer) + self.flush()

    def _release(self, text: str) -> str:
        self._done = True
        self._buffer = ""
        return self._scan(text)

    def _scan(self, text: str) -> str:
        if self._ended:
            return ""
        text, self._held = self._held + text, ""
        if match := ANY_TAG.search(text):
            self.tag = self.tag or match.group(1)
            self._ended = True
            return text[: match.start()].rstrip()
        start = text.rfind("[[")
        if start == -1 and text.endswith("["):
            start = len(text) - 1
        if start != -1 and any(t.startswith(text[start:]) for t in TAG_TEXTS):
            text, self._held = text[:start], text[start:]
        return text


class CitationFilter:
    """Keeps [n] markers that point at a provided chunk and drops all others, while streaming.

    Text that might be the start of a marker ("[", "[1", trailing spaces) is held back until it
    resolves, so markers split across chunks are handled and invalid ones never reach the
    customer. [1, 3] becomes [1][3]. Records which markers were used, in order.
    """

    MAX_MARKER = 16

    def __init__(self, valid: set[int]) -> None:
        self.valid = valid
        self.used: list[int] = []
        self.dropped: list[str] = []
        self._space = ""  # trailing whitespace not yet emitted
        self._marker = ""  # an open "[..." not yet resolved
        self._double = False  # the open marker started "[[" (some models write [[1]])
        self._skip_close = False  # swallow the second "]" of a resolved [[n]]

    def feed(self, text: str) -> str:
        out: list[str] = []
        for char in text:
            if self._skip_close:
                self._skip_close = False
                if char == "]":
                    continue
            if self._marker == "[" and char == "[" and not self._double:
                self._double = True
            elif self._marker:
                self._marker += char
                if char == "]":
                    out.append(self._resolve())
                elif not (char.isdigit() or char in ", ") or len(self._marker) > self.MAX_MARKER:
                    out.append(self._space + self._open() + self._marker)
                    self._space = self._marker = ""
            elif char == "[":
                self._marker = "["
            elif char.isspace():
                self._space += char
            else:
                out.append(self._space + char)
                self._space = ""
        return "".join(out)

    def _open(self) -> str:
        """The extra "[" of a "[[" marker, given back when the marker turns out to be text."""
        extra, self._double = ("[" if self._double else ""), False
        return extra

    def _resolve(self) -> str:
        marker, space, double = self._marker, self._space, self._double
        self._marker = self._space = ""
        numbers = [part for part in re.split(r"[,\s]+", marker[1:-1]) if part]
        if not numbers or not all(part.isdigit() for part in numbers):
            return space + self._open() + marker  # "[]" or "[note]": ordinary text
        self._double = False
        self._skip_close = double
        kept = [int(n) for n in numbers if int(n) in self.valid]
        if len(kept) != len(numbers):
            self.dropped.append(marker)
        if not kept:
            return ""  # drop the marker and the space before it
        for number in kept:
            if number not in self.used:
                self.used.append(number)
        return space + "".join(f"[{n}]" for n in kept)

    def flush(self) -> str:
        text = self._space + (self._open() if self._marker else "") + self._marker
        self._space = self._marker = ""
        return text


def decide_outcome(
    tag: str | None,
    cited: list[int],
    searched: bool,
    *,
    action: bool = False,
    lookup: bool = False,
) -> str:
    """The stored outcome, decided from what code can verify:

    - action: a write tool completed this turn (a reservation booked, a lead saved);
    - answered: the reply cites a source found in this conversation, or a record lookup (an
      order) succeeded this turn;
    - no_answer: a search was made but the reply cites nothing (nothing relevant was found, or
      the reply is not grounded), or the model reports it could not answer, or it claims an
      answer with nothing to verify it;
    - smalltalk: no search and no claim (the model's [[smalltalk]] tag, or no tag at all).
    """
    if action:
        return "action"
    if cited or lookup:
        return "answered"
    if searched:
        return "no_answer"
    if tag in (None, "smalltalk"):
        return "smalltalk"
    return "no_answer"
