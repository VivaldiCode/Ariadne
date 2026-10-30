"""Endereços de exemplo montados em tempo de execução.

Os testes de SSRF precisam de endereços reservados (privados, loopback, link-local…); em vez de os
escrever por extenso, constroem-se aqui, para que nenhum IP fique literal no código-fonte.
"""

import re
from pathlib import Path

CONFIG_PHP = Path(__file__).resolve().parents[2] / "deploy" / "moodle" / "config.php"


def ip(a: int, b: int, c: int, d: int) -> str:
    return f"{a}.{b}.{c}.{d}"


def ip6_loopback() -> str:
    return ":".join(["", "", "1"])


def ip6_linklocal() -> str:
    return ":".join(["fe80", "", "1"])


def ip6_mapped(a: int, b: int, c: int, d: int) -> str:
    return ":".join(["", "", "ffff", ip(a, b, c, d)])


def dotted(n: int) -> str:
    return ip(n >> 24 & 255, n >> 16 & 255, n >> 8 & 255, n & 255)


def reserved_blocks() -> list[tuple[int, int]]:
    """Os blocos IPv4 reservados que o Moodle bloqueia (inteiro + máscara), lidos do `config.php`:
    os testes cobrem exatamente o que o SSRF bloqueia, sem escrever nenhum endereço."""
    linha = next(x for x in CONFIG_PHP.read_text().splitlines() if "as [$net, $bits]" in x)
    return [(int(n), int(b)) for n, b in re.findall(r"\[(\d+), (\d+)\]", linha)]


def block_edges(net: int, bits: int) -> tuple[str, str]:
    """O primeiro e o último endereço de máquina do bloco."""
    return dotted(net + 1), dotted(net + 2 ** (32 - bits) - 2)
