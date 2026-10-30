"""Autenticidade da release: o `MANIFEST.json` assina-se com uma chave **própria de assinatura**
(ed25519, `ssh-keygen -Y sign`), distinta da chave age.

Sem isto, quem tivesse o token do GitHub (ou o próprio GitHub) podia trocar partes e manifesto por
outros coerentes: as impressões digitais só provam integridade, não autoria. A chave **pública** de
verificação fixa-se **fora do GitHub** (`RELEASE_SIGNING_PUBKEY`, no `.env`, no README e na mão de
quem restaura): o `restore.sh` e o `release verify` verificam a assinatura antes de qualquer outra
coisa e recusam se falhar, e nunca leem a chave pública do próprio arquivo. A privada de assinatura
(`RELEASE_SIGNING_KEY_FILE`) vive no servidor, porque é ele que assina: protege contra quem altera
a release depois, não contra um servidor comprometido (a chave age privada, essa, nunca lá está).
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

NAMESPACE = "ariadne-release"
SIGNER = "ariadne-release"
_PUBKEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]{40,}={0,2}")


class SignError(Exception):
    """Falta a chave, o `ssh-keygen`, ou a assinatura não confere."""


def normalize_pubkey(value: str) -> str:
    """`ssh-ed25519 AAAA…` (sem o comentário), validado: só ed25519, nenhuma linha extra."""
    words = value.strip().split()
    if len(words) < 2 or "\n" in value.strip():
        raise SignError('the signing public key must be "ssh-ed25519 AAAA…" (one line)')
    key = f"{words[0]} {words[1]}"
    if not _PUBKEY.fullmatch(key):
        raise SignError('the signing public key must be ed25519 ("ssh-ed25519 AAAA…")')
    return key


def _binary() -> str:
    found = shutil.which("ssh-keygen")
    if found is None:
        raise SignError("`ssh-keygen` (openssh-client) is missing, needed to sign/verify")
    return found


def sign_file(path: Path, key_file: Path) -> Path:
    """Assina `path` e devolve o `path.sig`. A chave copia-se para uma pasta temporária 0600 do
    processo (o `ssh-keygen` recusa chaves legíveis por outros; um volume montado pode estar assim)
    e apaga-se logo a seguir."""
    if not key_file.is_file():
        raise SignError(f"cannot find the signing key at {key_file}")
    sig = path.with_name(path.name + ".sig")
    sig.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="ariadne-sign-") as folder:
        key = Path(folder) / "key"
        key.write_bytes(key_file.read_bytes())
        key.chmod(0o600)
        done = subprocess.run(
            [_binary(), "-Y", "sign", "-q", "-f", str(key), "-n", NAMESPACE, str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    if done.returncode != 0 or not sig.is_file():
        raise SignError(f"ssh-keygen did not sign: {done.stderr.strip()[:200]}")
    return sig


def verify_file(path: Path, sig: Path, pubkey: str) -> None:
    """Levanta `SignError` se `sig` não é uma assinatura de `path` pela chave pública dada."""
    key = normalize_pubkey(pubkey)
    if not sig.is_file():
        raise SignError(f"the signature {sig.name} is missing")
    with tempfile.TemporaryDirectory(prefix="ariadne-verify-") as folder:
        allowed = Path(folder) / "allowed_signers"
        allowed.write_text(f'{SIGNER} namespaces="{NAMESPACE}" {key}\n')
        done = subprocess.run(
            [
                _binary(), "-Y", "verify", "-f", str(allowed), "-I", SIGNER,
                "-n", NAMESPACE, "-s", str(sig),
            ],
            input=path.read_bytes(),
            capture_output=True,
            check=False,
            timeout=60,
        )  # fmt: skip
    if done.returncode != 0:
        raise SignError("the manifest signature does not match the pinned public key")


def pubkey_of(key_file: Path) -> str:
    """A chave pública correspondente a uma privada de assinatura (para o autoteste)."""
    if not key_file.is_file():
        raise SignError(f"cannot find the signing key at {key_file} (mount the file)")
    with tempfile.TemporaryDirectory(prefix="ariadne-sign-") as folder:
        key = Path(folder) / "key"
        key.write_bytes(key_file.read_bytes())
        key.chmod(0o600)
        done = subprocess.run(
            [_binary(), "-y", "-f", str(key)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    if done.returncode != 0:
        raise SignError("could not read the signing key")
    return normalize_pubkey(done.stdout)
