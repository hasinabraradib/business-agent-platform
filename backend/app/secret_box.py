"""Encryption for secrets the platform must read back (e.g. Telegram bot tokens).

AES-256-GCM with SECRETS_ENCRYPTION_KEY. The tenant id is the associated data, so a ciphertext
copied into another tenant's row does not decrypt. Stored as "v1:<base64(nonce + ciphertext)>".
"""

import base64
import binascii
import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

PREFIX = "v1:"
NONCE_BYTES = 12


class SecretBoxError(RuntimeError):
    pass


def _key() -> bytes:
    raw = get_settings().secrets_encryption_key
    if not raw:
        raise SecretBoxError("SECRETS_ENCRYPTION_KEY is not set")
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SecretBoxError("SECRETS_ENCRYPTION_KEY must be base64") from exc
    if len(key) != 32:
        raise SecretBoxError("SECRETS_ENCRYPTION_KEY must be 32 bytes (base64-encoded)")
    return key


def encrypt(plaintext: str, tenant_id: uuid.UUID) -> str:
    nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(_key()).encrypt(nonce, plaintext.encode(), tenant_id.bytes)
    return PREFIX + base64.b64encode(nonce + sealed).decode()


def decrypt(token: str, tenant_id: uuid.UUID) -> str:
    if not token.startswith(PREFIX):
        raise SecretBoxError("unknown secret format")
    try:
        data = base64.b64decode(token[len(PREFIX) :], validate=True)
        opened = AESGCM(_key()).decrypt(data[:NONCE_BYTES], data[NONCE_BYTES:], tenant_id.bytes)
    except (binascii.Error, ValueError, InvalidTag) as exc:
        raise SecretBoxError("secret could not be decrypted") from exc
    return opened.decode()
