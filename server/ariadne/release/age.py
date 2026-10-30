"""Encriptação com o binário `age` (https://age-encryption.org).

O Ariadne só conhece a chave **pública** do destinatário (`RELEASE_AGE_RECIPIENT`, `age1…`): a
privada nunca está no sistema, por isso o servidor não consegue desencriptar as próprias
releases (esse é o objetivo). O binário corre num subprocesso (`age -r <chave>`), sem shell, com a
chave validada antes — nunca um argumento que comece por «-». A única chave privada que este
módulo toca é a de um par **descartável**, criado em memória para o autoteste (`selftest`), que
prova que o `age` instalado cifra e decifra antes de se confiar nele para uma release.
"""

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

# `age1…` (X25519, 62 caracteres). As chaves pós-quânticas (`age1pq1…`) pedem age ≥ 1.3 e a imagem
# traz o 1.1.1 (Debian): não se aceitam. Quem cifra de facto prova a chave com `encrypt_bytes`.
RECIPIENT = re.compile(r"age1[a-z0-9]{58}")
HEADER = b"age-encryption.org/v1"
SECRET_PREFIX = "AGE-SECRET-KEY-"


class AgeError(Exception):
    """O age não está instalado, a chave é inválida ou a cifra falhou."""


def check_recipient(recipient: str) -> str:
    """Devolve a chave pública `age1…` validada. Recusa uma chave PRIVADA posta por engano no
    sítio da pública (a privada nunca pode estar no sistema), e qualquer coisa que não seja uma
    chave age (palavra-passe, ssh, ficheiro)."""
    value = recipient.strip()
    if SECRET_PREFIX in value.upper():
        raise AgeError(
            "RELEASE_AGE_RECIPIENT holds a PRIVATE key (AGE-SECRET-KEY-…): remove it — the "
            "private key must never be on the system. Put only the public one (age1…)."
        )
    if not RECIPIENT.fullmatch(value):
        raise AgeError(
            "RELEASE_AGE_RECIPIENT is not a valid age public key (age1…, from `age-keygen`)."
        )
    return value


@dataclass
class Age:
    binary: str = "age"

    def path(self) -> str:
        found = shutil.which(self.binary)
        if found is None:
            raise AgeError(
                f"The `{self.binary}` binary is not installed (age; in the Ariadne image it comes "
                "from the Debian `age` package)."
            )
        return found

    def version(self) -> str:
        out = subprocess.run(
            [self.path(), "--version"], capture_output=True, text=True, check=False, timeout=20
        )
        return (out.stdout or out.stderr).strip() or "desconhecida"

    def start_encrypt(self, recipient: str) -> "subprocess.Popen[bytes]":
        """Arranca `age -r <chave>`: lê o texto claro do stdin e escreve o cifrado no stdout."""
        key = check_recipient(recipient)
        return subprocess.Popen(
            [self.path(), "-r", key],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def encrypt_bytes(self, recipient: str, data: bytes) -> bytes:
        key = check_recipient(recipient)
        done = subprocess.run(
            [self.path(), "-r", key], input=data, capture_output=True, check=False, timeout=120
        )
        if done.returncode != 0:
            raise AgeError(f"age failed: {done.stderr.decode(errors='replace')[:300]}")
        return done.stdout

    def decrypt_stream(
        self, identity: Path, source: IO[bytes], sink: IO[bytes]
    ) -> "subprocess.CompletedProcess[bytes]":
        """`age -d -i <identidade>`: só para ensaiar com a chave privada (fora do sistema)."""
        return subprocess.run(
            [self.path(), "-d", "-i", str(identity)],
            stdin=source,
            stdout=sink,
            stderr=subprocess.PIPE,
            check=False,
        )

    def keygen(self) -> tuple[str, str]:
        """(pública, linha da privada) de um par novo — usado só no autoteste e nos testes."""
        binary = shutil.which(f"{self.binary}-keygen") or shutil.which("age-keygen")
        if binary is None:
            raise AgeError("`age-keygen` is not installed.")
        out = subprocess.run([binary], capture_output=True, text=True, check=False, timeout=30)
        public = re.search(r"(age1[a-z0-9]+)", out.stderr + out.stdout)
        secret = re.search(r"(AGE-SECRET-KEY-[A-Z0-9]+)", out.stdout)
        if out.returncode != 0 or not public or not secret:
            raise AgeError("age-keygen did not return a key pair.")
        return public.group(1), secret.group(1)

    def selftest(self) -> str:
        """Cifra e decifra uma frase com um par descartável: prova que o `age` desta máquina
        funciona (e que cifra de facto: o cifrado começa pelo cabeçalho do age e não contém o texto
        claro). Devolve a versão. Falha com `AgeError`."""
        public, secret = self.keygen()
        probe = b"ariadne-autoteste-age"
        sealed = self.encrypt_bytes(public, probe)
        if not sealed.startswith(HEADER) or probe in sealed:
            raise AgeError("age did not encrypt the test text (output without the age header).")
        with tempfile.TemporaryDirectory(prefix="ariadne-age-") as folder:
            identity = Path(folder) / "descartavel.txt"
            identity.write_text(secret + "\n")
            identity.chmod(0o600)
            plain = subprocess.run(
                [self.path(), "-d", "-i", str(identity)],
                input=sealed,
                capture_output=True,
                check=False,
                timeout=60,
            )
        if plain.returncode != 0 or plain.stdout != probe:
            raise AgeError("age did not decrypt the test text.")
        return self.version()
