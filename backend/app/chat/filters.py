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


# Sign-offs the voice rules forbid. gpt-oss kept ending with "If you need more details, let us
# know." although the prompt said not to; the closing filter drops them in code.
CLOSINGS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^(?:and )?(?:if|should) you (?:need|have|want|would like)\b.*\b(?:let (?:us|me) know|"
        r"just ask|feel free|reach out|just say|happy to help|here to help)\b",
        r"^(?:please )?(?:feel free|don't hesitate|do not hesitate) to\b",
        r"^(?:just )?let (?:us|me) know if\b",
        r"^(?:is there )?anything else (?:i|we) can\b",
        r"^how else can (?:i|we)\b",
        r"^(?:i )?hope (?:this|that) helps\b",
        r"^(?:we're|we are|i'm|i am) (?:always )?here (?:to help|if you need)\b",
        r"\b(?:ar|aro|onno )?kichu (?:lagle|dorkar hole|jante chaile|proyojon hole)\b.*"
        r"\b(?:janaben|bolben|jiggesh korben|message korben)\b",
        r"(?:আর |আরও |অন্য )?কিছু (?:লাগলে|জানতে চাইলে|দরকার হলে|প্রয়োজন হলে).*(?:জানাবেন|বলবেন)",
    )
]
SENTENCE_END = re.compile(r"[.!?\u0964]+[\"')\]]*\s+|\n+")
TRAILING_END = re.compile(r"[.!?\u0964\"')\]]+$")  # may end a sentence once a space follows


def is_closing(sentence: str) -> bool:
    text = sentence.strip().strip("!.?\u0964 ")
    return bool(text) and any(pattern.search(text) for pattern in CLOSINGS)


# How sign-offs start. Only a sentence that may still turn into one is held back; every other
# sentence streams token by token as before.
CLOSING_STARTS = (
    "if you", "and if you", "should you", "feel free", "please feel", "please don't",
    "please do not", "don't hesitate", "do not hesitate", "let us know", "let me know",
    "just let", "anything else", "is there anything", "how else", "hope this", "hope that",
    "i hope", "we're here", "we are here", "i'm here", "i am here", "we're always",
    "ar kichu", "aro kichu", "onno kichu", "kichu lagle", "kichu dorkar", "kichu jante",
    "apnar jonne kichu", "আর কিছু", "আরও কিছু", "অন্য কিছু", "কিছু লাগলে", "কিছু দরকার",
    "কিছু জানতে",
)  # fmt: skip
MIN_DECIDE_CHARS = 3


def _may_be_closing(start: str) -> bool:
    text = " ".join(start.casefold().split())
    return any(c.startswith(text) or text.startswith(c) for c in CLOSING_STARTS)


class ClosingFilter:
    """Drops a trailing sign-off ("If you need anything else, just let us know.").

    A sentence that starts like a sign-off is held until it ends; a finished sign-off is held
    until more of the reply follows it (then it is released). Held sign-offs at the end of the
    reply are dropped, unless the reply is nothing but a sign-off. Other sentences stream as
    they arrive.
    """

    def __init__(self) -> None:
        self._buffer = ""  # the current sentence, not yet released
        self._streaming = False  # the current sentence can't be a sign-off: pass it through
        self._held: list[str] = []  # finished sign-offs (and blank lines after them)
        self._released = False

    def _emit(self, text: str, out: list[str]) -> None:
        if text:
            out.append("".join(self._held) + text)
            self._held = []
            self._released = self._released or bool(text.strip())

    def feed(self, text: str) -> str:
        self._buffer += text
        out: list[str] = []
        while self._buffer:
            match = SENTENCE_END.search(self._buffer)
            if self._streaming:
                if match is None:
                    # Keep trailing punctuation: if a space follows, the sentence has ended and
                    # the next one must be checked for a sign-off.
                    hold = TRAILING_END.search(self._buffer)
                    cut = hold.start() if hold else len(self._buffer)
                    self._emit(self._buffer[:cut], out)
                    self._buffer = self._buffer[cut:]
                    break
                self._emit(self._buffer[: match.end()], out)
                self._buffer, self._streaming = self._buffer[match.end() :], False
                continue
            if match is not None:
                sentence, self._buffer = self._buffer[: match.end()], self._buffer[match.end() :]
                if is_closing(sentence) or (self._held and not sentence.strip()):
                    self._held.append(sentence)
                else:
                    self._emit(sentence, out)
                continue
            start = self._buffer.lstrip()
            if len(start) < MIN_DECIDE_CHARS or _may_be_closing(start):
                break  # wait for more text
            self._streaming = True
        return "".join(out)

    def flush(self) -> str:
        tail, held = self._buffer, "".join(self._held)
        self._buffer, self._held, self._streaming = "", [], False
        if tail.strip() and not is_closing(tail):
            return held + tail
        if not self._released:
            return held + tail  # the reply is only a sign-off: keep it
        return ""


class PlainTextFilter:
    """The widget shows plain text: drops Markdown bold markers ("**") and turns non-breaking
    hyphens back into "-" (gpt-oss wrote booking references as "R\u2011K7C7FM", which a
    customer cannot copy or search for)."""

    def __init__(self) -> None:
        self._star = False  # a "*" at the end of the last piece, waiting for its pair

    def feed(self, text: str) -> str:
        text = ("*" if self._star else "") + text.replace("\u2010", "-").replace("\u2011", "-")
        self._star = text.endswith("*") and not text.endswith("**")
        if self._star:
            text = text[:-1]
        return text.replace("**", "")

    def flush(self) -> str:
        star, self._star = self._star, False
        return "*" if star else ""


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
    proposed: bool = False,
    handoff: bool = False,
    identity: bool = False,
) -> str:
    """The stored outcome, decided from what code can verify:

    - handoff: the conversation was handed to a person this turn (request_human);
    - action: a write tool completed this turn (a reservation booked, a lead saved);
    - answered: the reply cites a source found in this conversation, or a record lookup (an
      order) succeeded this turn;
    - no_answer: a search was made but the reply cites nothing (nothing relevant was found, or
      the reply is not grounded), or the model reports it could not answer, or it claims an
      answer with nothing to verify it;
    - smalltalk: no search and no claim (the model's [[smalltalk]] tag, or no tag at all);
      collecting details (a write tool's proposal read back for confirmation, or a tool call
      that was missing details); or answering "are you a real person?" (identity), which is
      not a knowledge gap.
    """
    if handoff:
        return "handoff"
    if action:
        return "action"
    if cited or lookup:
        return "answered"
    if searched:
        return "no_answer"
    if proposed or identity:
        return "smalltalk"
    if tag in (None, "smalltalk"):
        return "smalltalk"
    return "no_answer"
