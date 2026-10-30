"""Cifra do cofre: AES-256-GCM da biblioteca `cryptography` (OpenSSL: sem exigir AVX).

A chave mestra é uma só (`ARIADNE_MASTER_KEY`): 32 bytes em base64 (ou 64 caracteres hex). Cada
valor leva o seu nonce aleatório e o **nome da definição como dados associados** (AAD): mover o
texto cifrado de uma linha para outra do cofre faz a decifra falhar.

Formato guardado: `0x01 || nonce(12) || texto cifrado + etiqueta`.
"""

import base64
import binascii
import contextlib
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT_VERSION = b"\x01"
KEY_BYTES = 32
NONCE_BYTES = 12


class VaultError(Exception):
    """Qualquer falha do cofre cuja mensagem se pode mostrar a quem administra."""


class MasterKeyMissing(VaultError):
    pass


class MasterKeyInvalid(VaultError):
    pass


class MasterKeyWrong(VaultError):
    pass


def generate_master_key() -> str:
    """Uma chave mestra nova (base64 de 32 bytes aleatórios)."""
    return base64.b64encode(os.urandom(KEY_BYTES)).decode("ascii")


def parse_master_key(raw: str) -> bytes:
    """Os 32 bytes da chave mestra, ou `MasterKeyMissing`/`MasterKeyInvalid`."""
    text = raw.strip()
    if not text:
        raise MasterKeyMissing(
            "The vault master key is missing: set ARIADNE_MASTER_KEY "
            "(generate one with `ariadne secrets generate-key`)."
        )
    candidates: list[bytes] = []
    if len(text) == 2 * KEY_BYTES:
        with contextlib.suppress(ValueError):
            candidates.append(bytes.fromhex(text))
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            candidates.append(decoder(text + "=" * (-len(text) % 4)))
        except (binascii.Error, ValueError):
            continue
    for candidate in candidates:
        if len(candidate) == KEY_BYTES:
            return candidate
    raise MasterKeyInvalid(
        "ARIADNE_MASTER_KEY is not valid: it must be 32 bytes in base64 (or 64 hex characters). "
        "Generate one with `ariadne secrets generate-key`."
    )


def encrypt(key: bytes, name: str, plaintext: str) -> bytes:
    nonce = secrets.token_bytes(NONCE_BYTES)
    body = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), name.encode("utf-8"))
    return FORMAT_VERSION + nonce + body


def decrypt(key: bytes, name: str, blob: bytes) -> str:
    """O texto em claro; `MasterKeyWrong` se a chave não é a que cifrou (ou o dado foi alterado)."""
    if not blob.startswith(FORMAT_VERSION) or len(blob) < 1 + NONCE_BYTES + 16:
        raise VaultError(f"The vault value {name!r} is in an unknown format.")
    nonce = blob[1 : 1 + NONCE_BYTES]
    try:
        return AESGCM(key).decrypt(nonce, blob[1 + NONCE_BYTES :], name.encode("utf-8")).decode()
    except InvalidTag as exc:
        raise MasterKeyWrong(
            "The master key (ARIADNE_MASTER_KEY) is not the one that encrypted the vault. "
            "Restore the right key; if you lost it, the vault's values cannot be recovered "
            "(see a documentação de operação)."
        ) from exc


def hint(value: str) -> str:
    """Os últimos 4 caracteres, para reconhecer um segredo sem o mostrar (valores curtos: nada)."""
    return value[-4:] if len(value) >= 12 else ""


def random_secret(nbytes: int = 24) -> str:
    """Um valor aleatório em hex (`openssl rand -hex 24`): só letras e números."""
    return secrets.token_hex(nbytes)
