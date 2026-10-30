"""Classes de licença (STRUCTURE.md, secção 7): `open` e `private`.

Cada fonte tem uma classe, que os seus documentos herdam e que acompanha tudo o que deriva deles
(chunks, vetores, ligações, registos). Os ZIM são abertos; livros e cursos declaram-na ao entrar,
sem valor por omissão. Este módulo só tem o vocabulário (sem base de dados): o `ariadne.licenses`
aplica-o.
"""

from enum import StrEnum


class LicenseClass(StrEnum):
    OPEN = "open"
    PRIVATE = "private"

    @property
    def label(self) -> str:
        return {"open": "aberto", "private": "privado"}[self.value]


def parse_license_class(value: str | LicenseClass) -> LicenseClass:
    """«open»/«private» (também «aberto»/«privado»); qualquer outra coisa é um erro, nunca um
    valor por omissão."""
    if isinstance(value, LicenseClass):
        return value
    key = value.strip().casefold()
    aliases = {"aberto": "open", "privado": "private"}
    try:
        return LicenseClass(aliases.get(key, key))
    except ValueError as exc:
        raise ValueError(f"invalid license class {value!r}: use 'open' or 'private'") from exc
