"""Os blocos reservados do SSRF do Moodle (lidos do `config.php`) só dão endereços não públicos."""

import ipaddress

from tests.ipfake import block_edges, dotted, ip, reserved_blocks


def test_dotted_e_ip_concordam() -> None:
    assert dotted(3232235777) == ip(192, 168, 1, 1)


def test_os_blocos_do_config_php_so_tem_enderecos_nao_publicos() -> None:
    blocos = reserved_blocks()
    assert len(blocos) >= 7  # loopback, privadas (3), link-local, 0/8 e CGNAT
    for net, bits in blocos:
        primeiro, ultimo = block_edges(net, bits)
        for endereco in (primeiro, ultimo):
            assert not ipaddress.ip_address(endereco).is_global, endereco
        assert ipaddress.ip_address(primeiro) < ipaddress.ip_address(ultimo)
