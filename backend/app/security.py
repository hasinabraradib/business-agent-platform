"""API key generation and hashing. Only the sha256 hash of a key is ever stored."""

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass

from app.models import ApiKey, ApiKeyKind

KEY_RANDOM_BYTES = 32
# token_urlsafe(32) is always 43 characters from the URL-safe base64 alphabet.
API_KEY_PATTERN = re.compile(r"^bap_(admin|widget)_[A-Za-z0-9_-]{43}$")
PREFIX_LENGTH = 8


@dataclass(frozen=True)
class NewApiKey:
    record: ApiKey
    full_key: str  # show once to the caller; never store or log it


def hash_api_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode()).hexdigest()


def is_well_formed_api_key(value: str) -> bool:
    return API_KEY_PATTERN.fullmatch(value) is not None


def new_api_key(tenant_id: uuid.UUID, kind: ApiKeyKind, label: str = "") -> NewApiKey:
    """Build (but do not persist) an ApiKey row and its one-time plaintext key."""
    random_part = secrets.token_urlsafe(KEY_RANDOM_BYTES)
    full_key = f"bap_{kind}_{random_part}"
    record = ApiKey(
        tenant_id=tenant_id,
        kind=kind,
        # The fixed "bap_<kind>_" text would make every prefix identical, so the display prefix
        # is the first characters of the random part.
        prefix=random_part[:PREFIX_LENGTH],
        key_hash=hash_api_key(full_key),
        label=label,
    )
    return NewApiKey(record=record, full_key=full_key)
