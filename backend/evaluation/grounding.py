"""Groundedness: every checkable factual claim in a reply must appear in its sources.

Claims are found with patterns, not a model, so the check is deterministic and free:
prices, clock times, quantities and durations, dates, phone numbers, order or booking statuses,
and delivery estimates in sentences about an order or parcel. Sources are the cited chunks, the
tool results the model saw, staff messages, the tenant's settings text and the customer's own
messages (echoing the customer's party size is not an invention).

Not checked: claims without a number or a status word (e.g. "all meat is halal"). The report
says so rather than counting them as supported.
"""

import re
from dataclasses import dataclass, field

BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
MARKER = re.compile(r"\s?\[\d+\]")
NUMBER = r"\d+(?:\.\d+)?"

MONEY = re.compile(
    rf"(?:৳|\bbdt|\btk\.?|\btaka)\s*({NUMBER})|({NUMBER})\s*(?:৳|bdt\b|tk\b|tk\.|taka\w*|টাকা\w*)"
)
CLOCK = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)")
# "dupur 12 ta", "raat 11 tay", "দুপুর ১২টা", "রাত ১১ টা"
DAYPART = r"(?:shokal|sokal|dupur|bikel|shondha|sondha|raat|rat|সকাল|দুপুর|বিকেল|সন্ধ্যা|রাত)"
SPOKEN_TIME = re.compile(rf"{DAYPART}\s*(\d{{1,2}})(?:[:.](\d{{2}}))?\s*(?:ta|tay|tar|টা|টায়|টার)")
NOON = re.compile(r"\b(?:12\s*)?noon\b|\bmidday\b")
QUANTITY = re.compile(
    rf"\b({NUMBER})(?:\s*[\u2013-]\s*({NUMBER}))?\s*(%|percent\b|working days?\b|days?\b|hours?\b|"
    r"hrs?\b|minutes?\b|mins?\b|weeks?\b|km\b|kg\b|cm\b|inch(?:es)?\b|people\b|persons?\b|"
    r"guests?\b|jon\b|pieces?\b|pcs\b|cars?\b|দিন|জন|ঘণ্টা|মিনিট|সপ্তাহ)"
)
MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
DATE = re.compile(rf"\b(\d{{1,2}})\s*({MONTHS})[a-z]*|\b({MONTHS})[a-z]*\.?\s+(\d{{1,2}})\b")
ISO_DATE = re.compile(r"\b\d{4}-(\d{2})-(\d{2})\b")
PHONE = re.compile(r"\b0\d{4}[-\s]?\d{6}\b|\b0\d{3}[-\s]?\d{3}[-\s]?\d{4}\b")
STATUS = re.compile(
    r"\b(shipped|delivered|processing|dispatched|out for delivery|cancell?ed|refunded|"
    r"confirmed|booked)\b"
)
ORDER_CONTEXT = re.compile(
    r"\b(order|parcel|package|delivery|deliver\w*|courier|shipment|jl-\d+)\b|অর্ডার|পার্সেল"
)
ESTIMATE = re.compile(
    r"\b(arriv\w*|reach(?:es)? you|be with you|expected (?:on|by)|soon|tomorrow|today|"
    r"next week|in \d+ (?:working )?days?|within \d+ (?:working )?days?|pouche jabe|"
    r"peye jaben|druto)\b|(পৌঁছে যাবে|পেয়ে যাবেন|শীঘ্রই|আগামীকাল)"
)
SENTENCE = re.compile(r"[^.!?।\n]+[.!?।]?")


@dataclass(frozen=True)
class Claim:
    kind: str  # money | time | quantity | date | phone | status | estimate
    text: str  # as written in the reply
    value: str  # normalized value that must be found in the sources


@dataclass
class Grounding:
    claims: list[Claim] = field(default_factory=list)
    unsupported: list[Claim] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return not self.unsupported


def normalize(text: str) -> str:
    text = text.translate(BN_DIGITS).casefold()
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)  # 18,500 -> 18500
    return re.sub("[\u2010-\u2015\u2212]", "-", text).replace("\u202f", " ").replace("\xa0", " ")


def _hour12(hour: str, minute: str | None) -> str:
    h = int(hour) % 12 or 12
    return f"{h}:{int(minute or 0):02d}"


def _number(value: str) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


def extract_claims(reply: str) -> list[Claim]:
    text = normalize(MARKER.sub("", reply))
    claims: list[Claim] = []
    for m in MONEY.finditer(text):
        claims.append(Claim("money", m.group(0).strip(), _number(m.group(1) or m.group(2))))
    for m in CLOCK.finditer(text):
        claims.append(Claim("time", m.group(0), _hour12(m.group(1), m.group(2))))
    for m in SPOKEN_TIME.finditer(text):
        claims.append(Claim("time", m.group(0), _hour12(m.group(1), m.group(2))))
    for m in NOON.finditer(text):
        claims.append(Claim("time", m.group(0), "12:00"))
    for m in QUANTITY.finditer(text):
        for value in filter(None, (m.group(1), m.group(2))):
            claims.append(Claim("quantity", m.group(0), _number(value)))
    for m in DATE.finditer(text):
        day = m.group(1) or m.group(4)
        month = (m.group(2) or m.group(3))[:3]
        claims.append(Claim("date", m.group(0), f"{int(day)} {month}"))
    for m in PHONE.finditer(text):
        claims.append(Claim("phone", m.group(0), re.sub(r"\D", "", m.group(0))))
    for m in STATUS.finditer(text):
        claims.append(Claim("status", m.group(0), m.group(1).replace("canceled", "cancelled")))
    for sentence in SENTENCE.findall(text):
        if ORDER_CONTEXT.search(sentence):
            for m in ESTIMATE.finditer(sentence):
                phrase = m.group(0)
                stem = "arriv" if phrase.startswith("arriv") else phrase
                claims.append(Claim("estimate", phrase, stem))
    return claims


@dataclass
class SourceIndex:
    text: str
    numbers: set[str]
    times: set[str]
    dates: set[str]
    phones: set[str]


def index_sources(sources: list[str]) -> SourceIndex:
    text = normalize("\n".join(s for s in sources if s))
    numbers = {_number(n) for n in re.findall(NUMBER, text)}
    times = {_hour12(h, m) for h, m, _ in CLOCK.findall(text)}
    times |= {_hour12(h, m) for h, m in SPOKEN_TIME.findall(text)}
    times |= {_hour12(h, m) for h, m in re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)}
    if NOON.search(text):
        times.add("12:00")
    dates = {f"{int(d or day)} {(mo or mo2)[:3]}" for d, mo, mo2, day in DATE.findall(text)}
    month_names = MONTHS.split("|")
    dates |= {f"{int(day)} {month_names[int(month) - 1]}" for month, day in ISO_DATE.findall(text)}
    phones = {re.sub(r"\D", "", p) for p in PHONE.findall(text)}
    return SourceIndex(text, numbers, times, dates, phones)


def is_supported(claim: Claim, index: SourceIndex) -> bool:
    if claim.kind in ("money", "quantity"):
        return claim.value in index.numbers
    if claim.kind == "time":
        return claim.value in index.times
    if claim.kind == "date":
        return claim.value in index.dates
    if claim.kind == "phone":
        return claim.value in index.phones
    return claim.value in index.text  # status words and estimate phrases


def check(reply: str, sources: list[str]) -> Grounding:
    index = index_sources(sources)
    claims = extract_claims(reply)
    return Grounding(claims, [c for c in claims if not is_supported(c, index)])
