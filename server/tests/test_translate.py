"""Fase 7: tradução — proteger código, LibreTranslate, serviço com cache e degradação."""

import json
import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ariadne.config import Settings
from ariadne.db.models import TranslationCache
from ariadne.translate import protect
from ariadne.translate.base import TranslateError, TranslateUnavailable
from ariadne.translate.fake import FakeTranslator
from ariadne.translate.libretranslate import LibreTranslate
from ariadne.translate.service import (
    TranslateRequestError,
    TranslationService,
    normalize_code,
    split_chunks,
)

TERMS = protect.parse_terms("Python, Docker, pgvector, Wiki.js")


def settings(**overrides: object) -> Settings:
    return Settings(database_url="postgresql+psycopg://x@localhost/x", **overrides)  # type: ignore[arg-type]


# --- proteger e repor --------------------------------------------------------------------------

SAMPLES = [
    "Usa a função `read_document(document_id)` para ler o texto.",
    "Veja [doc:zim:wikipedia_pt/Lisboa] e também [doc:abc#2].",
    "Mais em https://pt.wikipedia.org/wiki/Revolu%C3%A7%C3%A3o_dos_Cravos_(1974), agora.",
    "Código:\n```python\nfor x in range(3):\n    print(x)  # imprime\n```\nFim do exemplo.",
    "O objeto os.path.join e a variável max_chars e a classe DocumentView e HTTPClient.",
    "Chama get_settings() e depois foo(a, b) ou Document.read(path).",
    "Instalar com Docker e Python, depois pgvector no PostgreSQL e Wiki.js.",
    "Ficheiros em /etc/ariadne/config.yml, ./run.sh e server/ariadne/app.py.",
    "ERROR_CODE e MAX_K_LIMIT são constantes.",
    "Sem nada de especial, só texto normal em português.",
    "",
]


@pytest.mark.parametrize("text", SAMPLES)
def test_proteger_e_repor_e_o_mesmo_texto(text: str) -> None:
    masked, saved = protect.protect(text, TERMS)

    assert protect.restore(masked, saved) == text
    for fragment in saved:
        assert fragment not in masked


def test_o_que_se_protege() -> None:
    _, saved = protect.protect(SAMPLES[0], TERMS)
    assert saved == ["`read_document(document_id)`"]
    _, saved = protect.protect(SAMPLES[1], TERMS)
    assert saved == ["[doc:zim:wikipedia_pt/Lisboa]", "[doc:abc#2]"]
    _, saved = protect.protect("(ver https://x.org/a_b) e https://x.org/c.", TERMS)
    assert saved == ["https://x.org/a_b", "https://x.org/c"]
    _, saved = protect.protect(SAMPLES[2], TERMS)
    assert saved == ["https://pt.wikipedia.org/wiki/Revolu%C3%A7%C3%A3o_dos_Cravos_(1974)"]
    masked, saved = protect.protect(SAMPLES[3], TERMS)
    assert len(saved) == 1 and saved[0].startswith("```python") and saved[0].endswith("```")
    assert "Código:" in masked and "Fim do exemplo." in masked
    _, saved = protect.protect(SAMPLES[4], TERMS)
    assert saved == ["os.path.join", "max_chars", "DocumentView", "HTTPClient"]
    _, saved = protect.protect(SAMPLES[5], TERMS)
    assert saved == ["get_settings()", "foo(a, b)", "Document.read(path)"]
    _, saved = protect.protect(SAMPLES[6], TERMS)
    assert saved == [
        "Docker",
        "Python",
        "pgvector",
        "PostgreSQL",
        "Wiki.js",
    ]  # PostgreSQL: camelCase
    _, saved = protect.protect(SAMPLES[7], TERMS)
    assert saved == ["/etc/ariadne/config.yml", "./run.sh", "server/ariadne/app.py"]


def test_texto_comum_fica_de_fora() -> None:
    masked, saved = protect.protect(
        "Quando foi a Revolução dos Cravos? Em abril, ou/e maio.", TERMS
    )
    assert saved == [] and masked.startswith("Quando foi")


def test_restore_recusa_marcadores_perdidos_repetidos_ou_inventados() -> None:
    masked, saved = protect.protect("Use `a_b()` e https://x.org/y agora", TERMS)
    assert len(saved) == 2
    lost = masked.replace("QXZ0001", "")
    with pytest.raises(TranslateError, match="lost"):
        protect.restore(lost, saved)
    with pytest.raises(TranslateError, match="repeated"):
        protect.restore(masked + " QXZ0000", saved)
    with pytest.raises(TranslateError, match="invented"):
        protect.restore(masked + " QXZ0009", saved)
    assert protect.restore(masked.replace("QXZ0000", "qxz 0000"), saved).startswith("Use `a_b()`")


def test_has_words_ignora_marcadores_e_numeros() -> None:
    assert not protect.has_words("QXZ0000 QXZ0001 123 \n")
    assert protect.has_words("QXZ0000 e depois")


# --- LibreTranslate ----------------------------------------------------------------------------


def lt_client(handler: httpx.MockTransport) -> LibreTranslate:
    return LibreTranslate(
        "http://lt:5000/", api_key="segredo", client=httpx.Client(transport=handler)
    )


def test_libretranslate_pede_e_le_a_resposta() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"translatedText": "olá"})

    out = lt_client(httpx.MockTransport(handler)).translate("hello", "en", "pt")

    assert out == "olá"
    (request,) = seen
    assert str(request.url) == "http://lt:5000/translate"
    assert json.loads(request.content) == {
        "q": "hello", "source": "en", "target": "pt", "format": "text", "api_key": "segredo",
    }  # fmt: skip


@pytest.mark.parametrize(
    ("response", "error", "message"),
    [
        (httpx.Response(400, json={"error": "xx is not supported"}), TranslateError, "xx is not"),
        (httpx.Response(500, text="boom"), TranslateUnavailable, "500"),
        (httpx.Response(200, text="não é json"), TranslateError, "Unexpected response"),
        (httpx.Response(200, json={"translatedText": 3}), TranslateError, "Unexpected response"),
        (httpx.Response(200, json={"outro": 1}), TranslateError, "Unexpected response"),
    ],
)
def test_libretranslate_erros(
    response: httpx.Response, error: type[Exception], message: str
) -> None:
    client = lt_client(httpx.MockTransport(lambda request: response))

    with pytest.raises(error, match=message):
        client.translate("x", "en", "pt")


def test_libretranslate_inacessivel() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("recusado")

    with pytest.raises(TranslateUnavailable, match="unreachable"):
        lt_client(httpx.MockTransport(down)).translate("x", "en", "pt")


# --- partir em pedaços -------------------------------------------------------------------------


def test_split_chunks_junta_de_volta_o_texto() -> None:
    paragraphs = [
        "Primeiro parágrafo. Com frases. E mais uma.",
        "Segundo.",
        "Terceiro, mais longo " * 5,
    ]
    text = "\n\n".join(paragraphs) + "\n\n\n  Fim."
    for limit in (10, 40, 90, 1000):
        chunks = split_chunks(text, limit)
        assert "".join(chunks) == text
        assert all(len(c) <= limit for c in chunks), limit
    assert split_chunks("curto", 100) == ["curto"]


# --- serviço -----------------------------------------------------------------------------------


def service(translator: FakeTranslator | None, **overrides: object) -> TranslationService:
    return TranslationService(translator, None, settings(**overrides))


def test_traduz_e_repoe_o_codigo_na_ida_e_volta() -> None:
    fake = FakeTranslator()
    svc = service(fake)
    text = "Veja o exemplo: a função `get_settings()` em https://x.org/a, Python e [doc:z/a]."

    there = svc.translate(text, "en", source="pt")
    back = svc.translate(there.text, "pt", source="en")

    assert there.translated
    assert "`get_settings()`" in there.text and "https://x.org/a" in there.text
    assert "Python" in there.text and "[doc:z/a]" in there.text
    assert "`get_settings()`" in back.text and "[doc:z/a]" in back.text
    # O motor nunca viu o código: só os marcadores.
    for sent, _, _ in fake.calls:
        assert "get_settings" not in sent and "https://" not in sent and "[doc:" not in sent
        assert "QXZ0000" in sent


def test_bloco_de_codigo_inteiro_nao_chega_ao_motor() -> None:
    fake = FakeTranslator()
    text = "Exemplo:\n```python\nprint('olá mundo')\n```\n"

    result = service(fake).translate(text, "en", source="pt")

    assert "```python\nprint('olá mundo')\n```" in result.text
    assert "olá" not in fake.calls[0][0] and "print" not in fake.calls[0][0]


def test_texto_so_de_codigo_nao_chama_o_motor() -> None:
    fake = FakeTranslator()

    result = service(fake).translate("`a_b()`", "en", source="pt")

    assert (result.text, result.translated, fake.calls) == ("`a_b()`", True, [])


def test_mesma_lingua_nao_traduz() -> None:
    fake = FakeTranslator()

    result = service(fake).translate("Quando foi a revolução dos cravos?", "pt")

    assert (result.translated, result.source, result.warning) == (False, "pt", None)
    assert fake.calls == []


def test_lingua_de_origem_automatica_quando_nao_se_decide() -> None:
    fake = FakeTranslator()

    result = service(fake).translate("日本の首都", "en")

    assert result.source == "auto" and fake.calls[0][1:] == ("auto", "en")


def test_marcadores_perdidos_degradam_com_aviso() -> None:
    fake = FakeTranslator()
    fake.lose_markers = True
    text = "Veja a função `get_settings()` agora."

    result = service(fake).translate(text, "en", source="pt")

    assert (result.translated, result.text) == (False, text)  # o texto original, intacto
    assert result.warning is not None and "markers" in result.warning


def test_servico_em_baixo_degrada_e_nao_insiste(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTranslator()
    fake.down = True
    svc = service(fake, translate_down_seconds=60.0)

    first = svc.translate("Quando foi a revolução", "en", source="pt")
    second = svc.translate("Quando foi a revolução", "en", source="pt")

    assert first.translated is False and first.warning and "unavailable" in first.warning
    assert second.warning is not None and "failed a moment ago" in second.warning
    assert len(fake.calls) == 1  # a 2.ª nem tentou


def test_sem_url_a_traducao_esta_desligada() -> None:
    svc = service(None)

    result = svc.translate("Olá mundo", "en", source="pt")

    assert (result.translated, result.text) == (False, "Olá mundo")
    assert result.warning is not None and "LIBRETRANSLATE_URL" in result.warning
    assert svc.translate_query("revolução", "pt", "en") == (None, None)  # desligado: sem aviso


def test_pedidos_invalidos() -> None:
    svc = service(FakeTranslator(), translate_max_chars=50)

    with pytest.raises(TranslateRequestError, match="empty"):
        svc.translate("   ", "en")
    with pytest.raises(TranslateRequestError, match="maximum is 50"):
        svc.translate("x" * 51, "en")
    with pytest.raises(TranslateRequestError, match="Invalid language code"):
        svc.translate("olá", "português")
    assert [normalize_code(c) for c in ("PT", "pt-br", "zh-hant", "EN")] == [
        "pt", "pt-BR", "zh-Hant", "en",
    ]  # fmt: skip


def test_codigo_de_portugues_configuravel_para_um_modelo_pt_pt() -> None:
    fake = FakeTranslator({("en", "pt-PT"): {"capital": "capital"}})
    svc = service(fake, libretranslate_pt_code="pt-PT")

    svc.translate("the capital", "pt", source="en")
    svc.translate("a capital", "en", source="pt")

    assert [(c[1], c[2]) for c in fake.calls] == [("en", "pt-PT"), ("pt-PT", "en")]


def test_pergunta_traduzida() -> None:
    svc = service(FakeTranslator())

    assert svc.translate_query("Quando foi a Revolução dos Cravos", "pt", "en") == (
        "When was the Revolution of the Carnations",
        None,
    )
    assert svc.translate_query("Lisboa", "pt", "en") == (None, None)  # nada mudou: nada a pesquisar
    off = service(FakeTranslator(), translate_queries=False)
    assert off.translate_query("Quando foi a Revolução", "pt", "en") == (None, None)
    broken = FakeTranslator()
    broken.down = True
    text, warning = service(broken).translate_query("Quando foi a Revolução", "pt", "en")
    assert text is None and warning is not None and "unavailable" in warning


def test_textos_grandes_vao_por_pedacos() -> None:
    fake = FakeTranslator()
    text = "\n\n".join(f"O exemplo {i} é um teste." for i in range(30))

    result = service(fake, translate_chunk_chars=100).translate(text, "en", source="pt")

    assert len(fake.calls) > 3 and all(len(c[0]) <= 100 for c in fake.calls)
    assert result.text.count("\n\n") == 29 and result.text.startswith("The example 0 is a test.")


# --- cache -------------------------------------------------------------------------------------

pytestmark_db = pytest.mark.db


@pytest.fixture
def cached(db: sessionmaker[Session]) -> Iterator[tuple[TranslationService, FakeTranslator]]:
    fake = FakeTranslator()
    yield TranslationService(fake, db, settings()), fake


@pytest.mark.db
def test_cache_evita_traduzir_duas_vezes(
    cached: tuple[TranslationService, FakeTranslator], db: sessionmaker[Session]
) -> None:
    svc, fake = cached
    first = svc.translate("Quando foi a revolução dos cravos?", "en", source="pt")
    second = svc.translate("Quando foi a revolução dos cravos?", "en", source="pt")

    assert (first.cached, second.cached) == (False, True)
    assert second.text == first.text and len(fake.calls) == 1
    other_target = svc.translate("Quando foi a revolução dos cravos?", "es", source="pt")
    assert other_target.cached is False and len(fake.calls) == 2
    with db() as session:
        assert session.scalar(select(func.count()).select_from(TranslationCache)) == 2
        row = session.scalars(
            select(TranslationCache).order_by(TranslationCache.created_at)
        ).first()
    assert row is not None and (row.source_lang, row.target_lang, row.backend) == (
        "pt",
        "en",
        "fake",
    )
    assert len(row.key) == 64  # sha256: o texto não fica guardado em claro na chave


@pytest.mark.db
def test_cache_falhada_nao_impede_traduzir(db: sessionmaker[Session]) -> None:
    class Broken:
        def __call__(self) -> Session:
            raise RuntimeError("base de dados em baixo")

    fake = FakeTranslator()
    svc = TranslationService(fake, Broken(), settings())  # type: ignore[arg-type]

    result = svc.translate("Quando foi a revolução", "en", source="pt")

    assert result.translated and result.text == "When was the revolution"


def test_termos_vazios_na_configuracao_usam_a_lista_de_omissao() -> None:
    svc = service(FakeTranslator(), translate_protected_terms=" ")
    masked, saved = protect.protect("Instalar Docker e Python", svc._terms)
    assert saved == ["Docker", "Python"] and masked.count("QXZ") == 2


# --- blocos grandes, marcadores com nonce, cache com validade -------------------------------


def test_bloco_de_codigo_maior_do_que_o_pedaco_e_um_atomo() -> None:
    code = "```python\n" + "\n".join(f"print('linha {i}')" for i in range(40)) + "\n```"
    text = f"O exemplo é um teste.\n\n{code}\n\nVeja o texto."

    chunks = split_chunks(text, 100)

    assert "".join(chunks) == text
    assert code in chunks  # inteiro, sozinho, maior do que o limite
    assert all(len(c) <= 100 for c in chunks if c != code)
    fake = FakeTranslator()
    result = service(fake, translate_chunk_chars=100).translate(text, "en", source="pt")
    assert result.translated and code in result.text
    assert all("linha" not in sent and "print" not in sent for sent, _, _ in fake.calls)


def test_cerca_sem_fecho_recusa_a_traducao_com_aviso() -> None:
    text = "O exemplo é um teste.\n\n```python\nprint('sem fecho')\n\nMais texto aqui."

    result = service(FakeTranslator(), translate_chunk_chars=30).translate(text, "en", source="pt")

    assert (result.translated, result.text) == (False, text)
    assert result.warning is not None and "cut" in result.warning


def test_literal_parecido_com_marcador_nao_faz_recusar_a_traducao() -> None:
    fake = FakeTranslator()
    text = "O teste usa `a_b()` e o texto QXZ0000 literal e QXZ0001."

    result = service(fake).translate(text, "en", source="pt")

    assert result.translated and "QXZ0000" in result.text and "`a_b()`" in result.text
    sent = fake.calls[0][0]
    assert "QXZ" in sent  # os literais lá estão como texto…
    marker = re.search(r"\b([A-Z]{3})0000\b", sent.replace("QXZ0000", ""))
    assert marker is not None and marker.group(1) != "QXZ"  # …e o marcador do código é outro
    assert protect.choose_prefix("sem nada") == "QXZ"


def test_prune_da_cache_por_validade_e_por_tamanho(db: sessionmaker[Session]) -> None:
    from datetime import UTC, datetime, timedelta

    from ariadne.translate.service import prune_cache

    now = datetime.now(UTC)
    with db() as session, session.begin():
        for index, age in enumerate((200, 100, 10, 5, 1)):
            session.add(
                TranslationCache(
                    key=f"k{index}",
                    source_lang="pt",
                    target_lang="en",
                    translated="x",
                    backend="fake",
                    created_at=now - timedelta(days=age),
                )
            )
    with db() as session, session.begin():
        assert prune_cache(session, ttl_days=90, max_rows=100) == 2  # k0 e k1 expiraram
    with db() as session, session.begin():
        assert (
            prune_cache(session, ttl_days=90, max_rows=2) == 1
        )  # passa do limite: sai a mais velha
        left = set(session.scalars(select(TranslationCache.key)))
    assert left == {"k3", "k4"}


def test_decay_limpa_a_cache_de_traducoes(db: sessionmaker[Session]) -> None:
    from datetime import UTC, datetime, timedelta

    from ariadne.services import Services
    from ariadne.sources.registry import SourceRegistry
    from ariadne.workers.decay import run_decay

    with db() as session, session.begin():
        session.add(
            TranslationCache(
                key="velha",
                source_lang="pt",
                target_lang="en",
                translated="x",
                backend="fake",
                created_at=datetime.now(UTC) - timedelta(days=400),
            )
        )
    services = Services(settings(), db, SourceRegistry(Path("/nao-existe")), None)  # type: ignore[arg-type]

    run_decay(services)

    with db() as session:
        assert session.scalar(select(func.count()).select_from(TranslationCache)) == 0


@pytest.mark.db
def test_a_chave_da_cache_inclui_o_codigo_de_portugues(db: sessionmaker[Session]) -> None:
    first = TranslationService(FakeTranslator(), db, settings())
    other = TranslationService(FakeTranslator(), db, settings(libretranslate_pt_code="pt-PT"))

    first.translate("a capital", "pt", source="en")
    again = other.translate("a capital", "pt", source="en")

    assert again.cached is False  # outro modelo de português: outra tradução
