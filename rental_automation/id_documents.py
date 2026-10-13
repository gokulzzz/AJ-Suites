"""Encryption helpers for occupant identity proof uploads."""

import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken

from rental_automation.config import get_settings


def _fernet() -> Fernet:
    key = get_settings().id_proof_encryption_key
    if not key:
        raise RuntimeError("Set ID_PROOF_ENCRYPTION_KEY in .env before uploading ID proofs.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError("ID_PROOF_ENCRYPTION_KEY is invalid; use a generated Fernet key.") from exc


def encrypt_id_proof(contents: bytes) -> tuple[bytes, str]:
    """Encrypt the uploaded bytes and return ciphertext plus an integrity digest."""
    return _fernet().encrypt(contents), hashlib.sha256(contents).hexdigest()


def decrypt_id_proof(ciphertext: bytes, expected_sha256: str) -> bytes:
    """Decrypt and verify a stored ID proof before it is downloaded."""
    try:
        contents = _fernet().decrypt(ciphertext)
    except InvalidToken as exc:
        raise RuntimeError("This ID proof cannot be decrypted with the configured key.") from exc
    if not hmac.compare_digest(hashlib.sha256(contents).hexdigest(), expected_sha256):
        raise RuntimeError("This ID proof failed its integrity check.")
    return contents
