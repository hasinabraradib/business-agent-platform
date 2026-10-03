"""Does a customer message read as confirming the details just read back to them?

Used by the confirmation gate for write tools, together with the requirement that the same
details were proposed in an earlier turn. Deliberately simple and conservative: an explicit yes
(English, Bengali, romanized Bengali) and no negation or request for changes.
"""

import re

AFFIRMATIVE = {
    # English
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed", "correct",
    "right", "perfect", "great", "fine", "please", "proceed", "book", "go",
    # romanized Bengali
    "ji", "jee", "jii", "ha", "haa", "hae", "hya", "hyan", "hmm", "thik", "korun", "koren",
    "koro", "kore", "den", "dao", "accha", "acha",
    # Bengali script
    "হ্যাঁ", "হ্যা", "জি", "জ্বি", "ঠিক", "আছে", "করুন", "করেন", "অবশ্যই", "আচ্ছা",
}  # fmt: skip
NEGATIVE = {
    "no", "not", "don't", "dont", "cancel", "wait", "change", "wrong", "stop", "instead",
    "actually", "but", "na", "nah", "nope", "noa", "bhul", "badle", "thamun",
    "না", "নাহ", "ভুল", "বাতিল", "বদল",
}  # fmt: skip


def is_confirmation(message: str) -> bool:
    # Split on spaces and punctuation, not \w: Bengali vowel signs are not \w characters.
    text = message.casefold().replace("\u2019", "'")
    words = [w for w in re.split(r"[\s,.!?;:()\"\u0964\-]+", text) if w]
    if not words or len(words) > 25:
        return False
    if any(word in NEGATIVE for word in words):
        return False
    return any(word in AFFIRMATIVE for word in words)
