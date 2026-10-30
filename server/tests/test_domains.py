"""Importação v2, domínios (docs/WIKI-IMPORT.md, secção 6): a árvore, os vetores, a colocação,
os caminhos e a API. Os vetores vêm de um embedder falso e determinístico."""

from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from ariadne.app import create_app
from ariadne.auth import PrincipalKind, Role, create_principal
from ariadne.config import Settings
from ariadne.db.models import (
    EMBEDDING_DIM,
    Chunk,
    ChunkVector,
    DocumentVector,
    DomainAssignment,
    DomainVector,
    Source,
    ToolCall,
)
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import Domain as DomainRow
from ariadne.embedder import FakeEmbedder
from ariadne.wiki import domains, paths
from ariadne.wiki.domains import (
    FALLBACK_ID,
    DomainConfig,
    DomainError,
    _Scored,
    assign_human,
    ensure_vectors,
    load_config,
    node_texts,
    parse_config,
    place,
    slugs_for,
    sync_tree,
)

MODEL = "fake/embedder@0000000"
AUTO = "transportes/automoveis"
RECEITAS = "alimentacao/receitas"
CONSERVACAO = "alimentacao/conservacao"


# --- o ficheiro (sem base de dados) --------------------------------------------------------------


def test_o_yaml_do_pacote_e_valido() -> None:
    config = load_config()
    ids = [n.id for n in config.nodes]
    assert len(ids) == len(set(ids))
    tops = [n for n in config.nodes if n.parent_id is None and n.id != FALLBACK_ID]
    assert 20 <= len(tops) <= 26
    assert FALLBACK_ID in ids
    for node in config.nodes:
        for key in (node.names, node.slugs, node.descriptions):
            assert set(key) == {"pt", "en"}
            assert all(value.strip() for value in key.values())
        if node.parent_id is not None:
            assert node.parent_id in ids


def test_a_arvore_cobre_os_dominios_pedidos() -> None:
    ids = {n.id for n in load_config().nodes}
    wanted_ids = (
        "saude/medicamentos", "saude/doencas", "saude/primeiros-socorros", "saude/nutricao",
        "saude/saude-mental", "saude/saude-reprodutiva", "alimentacao/receitas",
        "alimentacao/conservacao", "agricultura/solos-e-compostagem", "agricultura/culturas",
        "agricultura/pragas", "agricultura/pecuaria", "agricultura/apicultura",
        "agricultura/silvicultura", "agua", "energia/solar", "energia/eolica", "energia/hidrica",
        "energia/biomassa", "energia/eletricidade", "construcao/materiais", "construcao/tecnicas",
        "construcao/isolamento", "construcao/reparacoes", "transportes/automoveis",
        "transportes/estradas", "transportes/bicicletas", "transportes/navegacao", "eletronica",
        "informatica/programacao", "informatica/sistemas-operativos", "informatica/redes",
        "informatica/software", "informatica/seguranca", "ciencias/fisica", "ciencias/quimica",
        "ciencias/biologia", "ciencias/astronomia", "ciencias/geologia", "matematica",
        "engenharia/mecanica", "engenharia/metalurgia", "engenharia/ferramentas", "sobrevivencia",
        "geografia", "historia", "sociedade", "economia", "filosofia", "linguas/dicionarios",
        "linguas/gramatica", "linguas/obras", "artes/musica", "educacao", "desporto", "biografias",
    )  # fmt: skip
    for wanted in wanted_ids:
        assert wanted in ids, wanted


@pytest.mark.parametrize(
    ("source", "domain"),
    [
        ("mechanics.stackexchange.com_en_all", "transportes/automoveis"),
        ("diy.stackexchange.com_en_all", "construcao/reparacoes"),
        ("cooking.stackexchange.com_en_all", "alimentacao/receitas"),
        ("publicdomainrecipes.com_en_all", "alimentacao/receitas"),
        ("electronics.stackexchange.com_en_all", "eletronica"),
        ("ifixit_en_all", "eletronica"),
        ("wikem_en_all", "saude"),
        ("mdwiki_en_all", "saude"),
        ("nhs.uk_en_medicines", "saude"),
        ("medlineplus.gov_en_all", "saude"),
        ("zimgit-medicine_en", "saude"),
        ("devdocs_en_python", "informatica"),
        ("devdocs_pt_css", "informatica"),
        ("docs.python.org_en_all", "informatica/programacao"),
        ("openwrt.org_en_all", "informatica"),
        ("gentoo_en_all", "informatica"),
        ("alpinelinux_en_all", "informatica"),
        ("wikivoyage_en_all", "geografia"),
        ("theworldfactbook_en_all", "geografia/paises"),
        ("wiktionary_pt_all", "linguas/dicionarios"),
        ("zimgit-water_en", "agua"),
        ("zim:Wikem_EN_all", "saude"),
    ],
)
def test_priors_da_fonte(source: str, domain: str) -> None:
    prior = load_config().prior_for(source)
    assert prior is not None
    assert prior.domain == domain


def test_fontes_sem_prior() -> None:
    config = load_config()
    for source in ("wikipedia_pt_all", "wikihow_en_all", "lowtechmagazine.com_en_all", "ted_mul_x"):
        assert config.prior_for(source) is None, source
    # Um padrão sem `*` é um prefixo: o `wikem` apanha também «wikem_pt_all».
    assert config.prior_for("wikem_pt_all") is not None


def raw_yaml() -> dict[str, Any]:
    loaded = yaml.safe_load((Path(domains.__file__).with_name("domains.yaml")).read_text("utf-8"))
    assert isinstance(loaded, dict)
    return deepcopy(loaded)


def test_yaml_mal_formado_e_recusado() -> None:
    repeated = raw_yaml()
    repeated["domains"][1]["id"] = repeated["domains"][0]["id"]
    with pytest.raises(DomainError, match="repeated"):
        parse_config(repeated)

    no_english = raw_yaml()
    del no_english["domains"][0]["names"]["en"]
    with pytest.raises(DomainError, match="names missing"):
        parse_config(no_english)

    same_slug = raw_yaml()
    same_slug["domains"][0]["children"][1]["slugs"]["pt"] = same_slug["domains"][0]["children"][0][
        "slugs"
    ]["pt"]
    with pytest.raises(DomainError, match="slug"):
        parse_config(same_slug)

    bad_prior = raw_yaml()
    bad_prior["source_priors"].append({"pattern": "x", "domain": "nao/existe"})
    with pytest.raises(DomainError, match="priors"):
        parse_config(bad_prior)

    no_fallback = raw_yaml()
    no_fallback["domains"] = [d for d in no_fallback["domains"] if d["id"] != FALLBACK_ID]
    no_fallback["source_priors"] = []
    with pytest.raises(DomainError, match=FALLBACK_ID):
        parse_config(no_fallback)


# --- caminhos ------------------------------------------------------------------------------------


def test_caminho_da_pagina_no_dominio() -> None:
    path = paths.domain_page_path("pt", ["transportes", "automoveis"], "Bateria do Mercedes", "d")
    assert path == "transportes/automoveis/bateria-do-mercedes"
    # O locale não entra no caminho.
    assert paths.domain_page_path("en", ["transport", "cars"], "Brake pads", "d") == (
        "transport/cars/brake-pads"
    )
    assert paths.domain_page_path("pt", ["saude"], "", "zim:x/y").startswith("saude/")
    assert paths.domain_page_path("pt", ["saude"], "???", "zim:x/y") == (
        f"saude/{paths.short_hash('zim:x/y', 10)}"
    )
    assert paths.domain_page_path("pt", [], "Lisboa", "d") == "lisboa"


def test_caminho_do_indice_do_dominio() -> None:
    assert paths.domain_index_path(["transportes", "automoveis"]) == "transportes/automoveis"
    assert paths.domain_index_path(["Saúde"]) == "saude"
    assert paths.domain_index_path([]) == ""


def test_as_funcoes_antigas_continuam() -> None:
    assert paths.page_path("zim:wikipedia_pt_x", "pt", "Revolução dos Cravos", "d") == (
        "wikipedia-pt-x/pt/revolucao-dos-cravos"
    )
    assert paths.disambiguate("a/b", "d").startswith("a/b-")


# --- a tabela ------------------------------------------------------------------------------------


@pytest.fixture
def session(db: sessionmaker[Session]) -> Iterator[Session]:
    with db() as s:
        yield s


@pytest.fixture
def tree(session: Session) -> Session:
    sync_tree(session)
    session.commit()
    return session


def unit(*weights: float) -> list[float]:
    """Um vetor de 384 dimensões: os pesos nos primeiros eixos, normalizado."""
    vector = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    vector[: len(weights)] = weights
    return [float(x) for x in vector / np.linalg.norm(vector)]


def node_text(domain_id: str) -> list[str]:
    spec = next(n for n in load_config().nodes if n.id == domain_id)
    return node_texts(spec.names, spec.descriptions)


def embedder_for(**leaf_vectors: list[float]) -> FakeEmbedder:
    """Fixa o vetor de cada domínio (o nome do argumento troca `_` por `/`); os outros ficam com
    o vetor de hash do texto, quase ortogonal a tudo."""
    vectors: dict[str, list[float]] = {}
    for key, vector in leaf_vectors.items():
        for text in node_text(key.replace("__", "/")):
            vectors[text] = vector
    return FakeEmbedder(MODEL, vectors)


def add_document(session: Session, document_id: str, source_id: str = "zim:teste_en_all") -> None:
    if session.get(Source, source_id) is None:
        session.add(
            Source(
                id=source_id,
                kind="zim",
                title=source_id,
                version="1",
                wing="teste",
                license_class="open",
            )
        )
        session.flush()
    session.add(
        DocumentRow(
            id=document_id,
            source_id=source_id,
            path=document_id.split("/", 1)[-1],
            version="1",
            title=document_id,
            license_class="open",
        )
    )
    session.flush()


def put_vector(session: Session, document_id: str, vector: list[float]) -> None:
    session.merge(DocumentVector(document_id=document_id, model_id=MODEL, embedding=vector))
    session.flush()


@pytest.mark.db
def test_sync_tree_e_idempotente(session: Session) -> None:
    config = load_config()
    first = sync_tree(session)
    session.commit()
    assert first.created == len(config.nodes)
    assert (first.updated, first.deactivated) == (0, 0)
    assert session.scalar(select(func.count()).select_from(DomainRow)) == len(config.nodes)

    again = sync_tree(session)
    assert (again.created, again.updated, again.deactivated) == (0, 0, 0)

    auto = session.get(DomainRow, AUTO)
    assert auto is not None
    assert auto.parent_id == "transportes"
    assert auto.slugs == {"pt": "automoveis", "en": "cars"}
    assert auto.active


@pytest.mark.db
def test_sync_tree_desativa_o_que_saiu_e_reativa(session: Session) -> None:
    sync_tree(session)
    smaller = raw_yaml()
    smaller["domains"][0]["children"] = smaller["domains"][0]["children"][:-1]
    removed = "saude/saude-publica"
    result = sync_tree(session, parse_config(smaller))
    assert result.deactivated == 1
    row = session.get(DomainRow, removed)
    assert row is not None
    assert not row.active  # não se apaga
    assert result.created == 0

    back = sync_tree(session)
    assert back.updated == 1
    assert session.get(DomainRow, removed).active  # type: ignore[union-attr]


@pytest.mark.db
def test_mudar_uma_descricao_apaga_o_vetor_do_no(tree: Session) -> None:
    ensure_vectors(tree, embedder_for(), MODEL)
    tree.commit()
    changed = raw_yaml()
    changed["domains"][1]["children"][0]["descriptions"]["pt"] = "Receitas com outras palavras."
    sync_tree(tree, parse_config(changed))
    tree.commit()
    have = set(tree.scalars(select(DomainVector.domain_id)))
    assert RECEITAS not in have
    assert CONSERVACAO in have
    assert ensure_vectors(tree, embedder_for(), MODEL) == 1


@pytest.mark.db
def test_ensure_vectors_usa_o_prefixo_passage_e_e_idempotente(tree: Session) -> None:
    embedder = FakeEmbedder(MODEL)
    created = ensure_vectors(tree, embedder, MODEL)
    tree.commit()
    config = load_config()
    expected = len([n for n in config.nodes if n.id != FALLBACK_ID])
    assert created == expected
    assert tree.scalar(select(func.count()).select_from(DomainVector)) == expected
    assert tree.get(DomainVector, (FALLBACK_ID, MODEL)) is None
    assert {kind for kind, _ in embedder.calls} == {"passage"}  # o embedder põe o prefixo
    assert ensure_vectors(tree, embedder, MODEL) == 0
    assert len(embedder.calls) == 1

    with pytest.raises(DomainError, match="embedder"):
        ensure_vectors(tree, FakeEmbedder("outro/modelo@1"), MODEL)


@pytest.mark.db
def test_o_vetor_do_dominio_e_a_media_das_duas_linguas(tree: Session) -> None:
    pt, en = node_text(AUTO)
    embedder = FakeEmbedder(MODEL, {pt: unit(1, 0), en: unit(0, 1)})
    ensure_vectors(tree, embedder, MODEL)
    row = tree.get(DomainVector, (AUTO, MODEL))
    assert row is not None
    vector = np.asarray(row.embedding)
    assert vector[0] == pytest.approx(vector[1], abs=1e-5)
    assert float(np.linalg.norm(vector)) == pytest.approx(1.0, abs=1e-5)


@pytest.mark.db
def test_slugs_da_raiz_ao_no_com_reserva_em_ingles(tree: Session) -> None:
    assert slugs_for(tree, AUTO, "pt") == ["transportes", "automoveis"]
    assert slugs_for(tree, AUTO, "en") == ["transport", "cars"]
    assert slugs_for(tree, AUTO, "fr") == ["transport", "cars"]  # sem fr: o inglês
    assert slugs_for(tree, "agua", "pt") == ["agua-e-saneamento"]
    row = tree.get(DomainRow, AUTO)
    assert row is not None
    row.slugs = {"en": "cars"}  # só inglês
    tree.flush()
    assert slugs_for(tree, AUTO, "pt") == ["transportes", "cars"]
    with pytest.raises(DomainError):
        slugs_for(tree, "nao/existe", "pt")


# --- colocar -------------------------------------------------------------------------------------


def assignment(session: Session, document_id: str) -> DomainAssignment:
    session.expire_all()
    row = session.get(DomainAssignment, document_id)
    assert row is not None
    return row


@pytest.mark.db
def test_prior_de_uma_folha(tree: Session) -> None:
    add_document(tree, "zim:mechanics/q1")
    result = place(
        tree,
        document_id="zim:mechanics/q1",
        source_name="mechanics.stackexchange.com_en_all",
        embedder_model_id=MODEL,
    )
    assert (result.domain_id, result.method, result.needs_review) == (AUTO, "prior", False)
    assert result.confidence == pytest.approx(0.9)
    stored = assignment(tree, "zim:mechanics/q1")
    assert (stored.domain_id, stored.method) == (AUTO, "prior")
    assert stored.signals["prior"]["pattern"] == "mechanics.stackexchange"


@pytest.mark.db
def test_prior_de_um_dominio_com_filhos_os_vetores_escolhem_o_filho(tree: Session) -> None:
    drug = unit(1)
    embedder = embedder_for(saude__medicamentos=drug)
    ensure_vectors(tree, embedder, MODEL)
    add_document(tree, "zim:nhs/ibuprofen")
    put_vector(tree, "zim:nhs/ibuprofen", unit(0.97, 0.1, 0.2))
    result = place(
        tree,
        document_id="zim:nhs/ibuprofen",
        source_name="nhs.uk_en_medicines",
        embedder_model_id=MODEL,
    )
    assert (result.domain_id, result.method) == ("saude/medicamentos", "prior")
    assert result.signals["refined"]["top"][0]["domain"] == "saude/medicamentos"


@pytest.mark.db
def test_prior_de_um_dominio_com_filhos_sem_provas_fica_no_pai(tree: Session) -> None:
    add_document(tree, "zim:nhs/x")
    result = place(
        tree, document_id="zim:nhs/x", source_name="nhs.uk_en_medicines", embedder_model_id=MODEL
    )
    assert (result.domain_id, result.method) == ("saude", "prior")  # sem vetores: o pai
    ensure_vectors(tree, FakeEmbedder(MODEL), MODEL)
    put_vector(tree, "zim:nhs/x", unit(0, 0, 0, 0, 1))  # nada se parece
    again = place(
        tree, document_id="zim:nhs/x", source_name="nhs.uk_en_medicines", embedder_model_id=MODEL
    )
    assert again.domain_id == "saude"


@pytest.mark.db
def test_vetores_com_confianca(tree: Session) -> None:
    embedder = embedder_for(**{AUTO.replace("/", "__"): unit(1)})
    ensure_vectors(tree, embedder, MODEL)
    add_document(tree, "zim:wikipedia/carro")
    put_vector(tree, "zim:wikipedia/carro", unit(0.95, 0, 0, 0.3))
    result = place(
        tree,
        document_id="zim:wikipedia/carro",
        source_name="wikipedia_en_all",
        embedder_model_id=MODEL,
    )
    assert (result.domain_id, result.method, result.needs_review) == (AUTO, "vector", False)
    assert result.confidence > 0.5  # top1 menos top2
    signals = result.signals["vector"]
    assert signals["source"] == "document_vector"
    assert signals["top"][0]["domain"] == AUTO
    assert len(signals["top"]) == 3
    stored = assignment(tree, "zim:wikipedia/carro")
    assert (stored.domain_id, stored.method, stored.needs_review) == (AUTO, "vector", False)


@pytest.mark.db
def test_vetor_fraco_vai_para_revisao_mas_fica_no_melhor_dominio(tree: Session) -> None:
    ensure_vectors(tree, embedder_for(**{AUTO.replace("/", "__"): unit(1)}), MODEL)
    add_document(tree, "zim:wikipedia/talvez")
    put_vector(tree, "zim:wikipedia/talvez", unit(0.76, 0, 0, 0, 0.65))  # 0,76 < 0,80
    result = place(
        tree,
        document_id="zim:wikipedia/talvez",
        source_name="wikipedia_en_all",
        embedder_model_id=MODEL,
    )
    assert (result.domain_id, result.method, result.needs_review) == (AUTO, "vector", True)


@pytest.mark.db
def test_margem_curta_vai_para_revisao(tree: Session) -> None:
    near = unit(0.95, 0.312)  # cosseno 0,95 com o eixo 0
    embedder = embedder_for(**{AUTO.replace("/", "__"): unit(1), "transportes__bicicletas": near})
    ensure_vectors(tree, embedder, MODEL)
    add_document(tree, "zim:wikipedia/empate")
    put_vector(tree, "zim:wikipedia/empate", unit(0.85, 0.136, 0.509))  # 0,85 e 0,85
    result = place(
        tree,
        document_id="zim:wikipedia/empate",
        source_name="wikipedia_en_all",
        embedder_model_id=MODEL,
    )
    assert result.method == "vector"
    assert result.needs_review
    assert result.signals["vector"]["margin"] < 0.02


@pytest.mark.db
def test_a_estrutura_da_fonte_desempata_um_vetor_fraco(tree: Session) -> None:
    ensure_vectors(tree, embedder_for(**{AUTO.replace("/", "__"): unit(1)}), MODEL)
    add_document(tree, "zim:cd3wd/a")
    put_vector(tree, "zim:cd3wd/a", unit(0.76, 0, 0, 0, 0.65))
    result = place(
        tree,
        document_id="zim:cd3wd/a",
        source_name="cd3wdproject.org_en_all",
        embedder_model_id=MODEL,
        structure_hint=AUTO,
    )
    assert (result.domain_id, result.method, result.needs_review) == (AUTO, "structure", False)

    add_document(tree, "zim:cd3wd/b")
    put_vector(tree, "zim:cd3wd/b", unit(0.76, 0, 0, 0, 0.65))
    elsewhere = place(
        tree,
        document_id="zim:cd3wd/b",
        source_name="cd3wdproject.org_en_all",
        embedder_model_id=MODEL,
        structure_hint="agricultura/culturas",
    )
    assert (elsewhere.domain_id, elsewhere.method) == ("agricultura/culturas", "structure")
    assert elsewhere.needs_review  # os vetores não a apoiam


@pytest.mark.db
def test_sem_vetores_a_estrutura_serve_e_sem_ela_por_classificar(tree: Session) -> None:
    add_document(tree, "zim:cd3wd/c")
    hinted = place(
        tree,
        document_id="zim:cd3wd/c",
        source_name="cd3wdproject.org_en_all",
        embedder_model_id=MODEL,
        structure_hint="agricultura/culturas",
    )
    assert (hinted.method, hinted.needs_review) == ("structure", True)

    add_document(tree, "zim:cd3wd/d")
    lost = place(
        tree,
        document_id="zim:cd3wd/d",
        source_name="cd3wdproject.org_en_all",
        embedder_model_id=MODEL,
    )
    assert (lost.domain_id, lost.method, lost.needs_review) == (FALLBACK_ID, "fallback", True)
    assert lost.confidence == 0
    assert "vector" in lost.signals["vector"]
    assert assignment(tree, "zim:cd3wd/d").domain_id == FALLBACK_ID


@pytest.mark.db
def test_um_documento_que_nada_se_parece_fica_por_classificar(tree: Session) -> None:
    ensure_vectors(tree, FakeEmbedder(MODEL), MODEL)
    add_document(tree, "zim:wikipedia/estranho")
    put_vector(tree, "zim:wikipedia/estranho", unit(0, 0, 0, 0, 0, 1))
    result = place(
        tree,
        document_id="zim:wikipedia/estranho",
        source_name="wikipedia_en_all",
        embedder_model_id=MODEL,
    )
    assert (result.domain_id, result.method) == (FALLBACK_ID, "fallback")
    assert "floor" in result.signals


@pytest.mark.db
def test_domain_vectors_em_falta_nao_partem_a_colocacao(tree: Session) -> None:
    add_document(tree, "zim:wikipedia/sem-dominios")
    put_vector(tree, "zim:wikipedia/sem-dominios", unit(1))
    result = place(
        tree,
        document_id="zim:wikipedia/sem-dominios",
        source_name="wikipedia_en_all",
        embedder_model_id=MODEL,
    )
    assert result.domain_id == FALLBACK_ID
    assert "ensure_vectors" in result.signals["vector"]


@pytest.mark.db
def test_colocar_com_snapshot_da_o_mesmo_e_nao_le_a_arvore(tree: Session) -> None:
    ensure_vectors(tree, embedder_for(**{AUTO.replace("/", "__"): unit(1)}), MODEL)
    add_document(tree, "zim:wikipedia/snap")
    put_vector(tree, "zim:wikipedia/snap", unit(1, 0.1))
    snapshot = domains.load_snapshot(tree, MODEL)
    assert AUTO in snapshot.leaves and FALLBACK_ID not in snapshot.leaves
    assert set(snapshot.vectors) <= snapshot.leaves and AUTO in snapshot.vectors
    assert snapshot.under(AUTO, "transportes") and not snapshot.under(AUTO, "saude")
    assert "transportes/automoveis" in snapshot.index_paths_for("pt")
    assert "transport/cars" in snapshot.index_paths_for("en")
    assert snapshot.index_paths_for("es") == snapshot.index_paths_for("en")  # reserva em inglês

    kwargs = {"document_id": "zim:wikipedia/snap", "source_name": "wikipedia_en_all"}
    fresh = place(tree, embedder_model_id=MODEL, **kwargs)
    # Com a árvore e os vetores já carregados, estragar a base de dados já não muda nada.
    tree.execute(delete(DomainVector))
    tree.get(DomainRow, AUTO).active = False  # type: ignore[union-attr]
    tree.flush()
    cached = place(tree, embedder_model_id=MODEL, snapshot=snapshot, **kwargs)
    assert (cached.domain_id, cached.method) == (fresh.domain_id, fresh.method) == (AUTO, "vector")
    # Um snapshot de outro modelo não serve: recarrega-se (e para esse modelo não há vetores).
    other = place(tree, embedder_model_id="outro/modelo@1111111", snapshot=snapshot, **kwargs)
    assert other.method == "fallback"


@pytest.mark.db
def test_o_centroide_dos_chunks_serve_sem_vetor_de_artigo(tree: Session) -> None:
    ensure_vectors(tree, embedder_for(**{AUTO.replace("/", "__"): unit(1)}), MODEL)
    add_document(tree, "zim:wikipedia/chunks")
    for ord_, vector in enumerate([unit(1, 0.1), unit(1, -0.1)]):
        chunk = Chunk(document_id="zim:wikipedia/chunks", ord=ord_, text="x")
        tree.add(chunk)
        tree.flush()
        tree.add(ChunkVector(chunk_id=chunk.id, model_id=MODEL, embedding=vector))
    tree.flush()
    result = place(
        tree,
        document_id="zim:wikipedia/chunks",
        source_name="wikipedia_en_all",
        embedder_model_id=MODEL,
    )
    assert (result.domain_id, result.method) == (AUTO, "vector")
    assert result.signals["vector"]["source"] == "chunks"


@pytest.mark.db
def test_a_colocacao_humana_nunca_e_sobreposta(tree: Session) -> None:
    add_document(tree, "zim:mechanics/q9")
    first = place(
        tree,
        document_id="zim:mechanics/q9",
        source_name="mechanics.stackexchange.com_en_all",
        embedder_model_id=MODEL,
    )
    assert first.method == "prior"

    human = assign_human(
        tree,
        document_id="zim:mechanics/q9",
        domain_id="construcao/reparacoes",
        reviewer="7",
        note="é sobre o tejadilho",
    )
    assert (human.method, human.confidence, human.needs_review) == ("human", 1.0, False)
    assert human.signals["previous"]["method"] == "prior"

    again = place(
        tree,
        document_id="zim:mechanics/q9",
        source_name="mechanics.stackexchange.com_en_all",
        embedder_model_id=MODEL,
        structure_hint=AUTO,
    )
    assert (again.domain_id, again.method) == ("construcao/reparacoes", "human")
    stored = assignment(tree, "zim:mechanics/q9")
    assert (stored.domain_id, stored.method) == ("construcao/reparacoes", "human")
    assert stored.signals["note"] == "é sobre o tejadilho"


@pytest.mark.db
def test_a_colocacao_do_llm_tambem_sobrevive_a_vetores(tree: Session) -> None:
    add_document(tree, "zim:wikipedia/llm")
    tree.add(
        DomainAssignment(
            document_id="zim:wikipedia/llm",
            domain_id="historia/militar",
            confidence=0.95,
            method="llm",
            signals={},
            needs_review=False,
        )
    )
    tree.flush()
    result = place(
        tree,
        document_id="zim:wikipedia/llm",
        source_name="mechanics.stackexchange.com_en_all",
        embedder_model_id=MODEL,
    )
    assert (result.domain_id, result.method) == ("historia/militar", "llm")


@pytest.mark.db
def test_colocar_de_novo_atualiza_uma_colocacao_automatica(tree: Session) -> None:
    add_document(tree, "zim:cd3wd/e")
    place(
        tree,
        document_id="zim:cd3wd/e",
        source_name="cd3wdproject.org_en_all",
        embedder_model_id=MODEL,
    )
    assert assignment(tree, "zim:cd3wd/e").method == "fallback"
    place(
        tree,
        document_id="zim:cd3wd/e",
        source_name="cd3wdproject.org_en_all",
        embedder_model_id=MODEL,
        structure_hint="agricultura/culturas",
    )
    assert assignment(tree, "zim:cd3wd/e").domain_id == "agricultura/culturas"
    assert tree.scalar(select(func.count()).select_from(DomainAssignment)) == 1


@pytest.mark.db
def test_atribuicao_humana_recusa_o_que_nao_existe(tree: Session) -> None:
    add_document(tree, "zim:x/y")
    with pytest.raises(DomainError, match=r"[Dd]ocument"):
        assign_human(tree, document_id="zim:x/nao", domain_id=AUTO, reviewer="1")
    with pytest.raises(DomainError, match=r"[Dd]omain"):
        assign_human(tree, document_id="zim:x/y", domain_id="nao/existe", reviewer="1")
    row = tree.get(DomainRow, AUTO)
    assert row is not None
    row.active = False
    tree.flush()
    with pytest.raises(DomainError, match="inactive"):
        assign_human(tree, document_id="zim:x/y", domain_id=AUTO, reviewer="1")


@pytest.mark.db
def test_um_prior_para_um_dominio_inativo_e_ignorado(tree: Session) -> None:
    row = tree.get(DomainRow, AUTO)
    assert row is not None
    row.active = False
    tree.flush()
    add_document(tree, "zim:mechanics/z")
    result = place(
        tree,
        document_id="zim:mechanics/z",
        source_name="mechanics.stackexchange.com_en_all",
        embedder_model_id=MODEL,
    )
    assert result.method == "fallback"
    assert "prior_ignored" in result.signals


# --- a API ---------------------------------------------------------------------------------------


@pytest.fixture
def api(database_url: str, db: sessionmaker[Session], tmp_path: Path) -> Iterator[TestClient]:
    with db() as s, s.begin():
        sync_tree(s)
        add_document(s, "zim:mechanics.stackexchange/questions/1/bateria")
        add_document(s, "zim:mechanics.stackexchange/questions/2/travoes")
        add_document(s, "zim:cd3wd/AUTO.HTM")
        _, key = create_principal(s, PrincipalKind.user, "pessoa", Role.admin)
        _, agent = create_principal(s, PrincipalKind.agent, "agente")
        place(
            s,
            document_id="zim:mechanics.stackexchange/questions/1/bateria",
            source_name="mechanics.stackexchange.com_en_all",
            embedder_model_id=MODEL,
        )
        place(
            s,
            document_id="zim:mechanics.stackexchange/questions/2/travoes",
            source_name="mechanics.stackexchange.com_en_all",
            embedder_model_id=MODEL,
        )
        place(
            s,
            document_id="zim:cd3wd/AUTO.HTM",
            source_name="cd3wdproject.org_en_all",
            embedder_model_id=MODEL,
        )
    zims = tmp_path / "zim"
    zims.mkdir()
    settings = Settings(database_url=database_url, zim_dir=zims, kiwix_url="http://kiwix.invalido")
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {key}"}) as client:
        client.agent_key = agent  # type: ignore[attr-defined]
        yield client


def find(nodes: list[dict[str, Any]], domain_id: str) -> dict[str, Any]:
    for node in nodes:
        if node["id"] == domain_id:
            return node
        if found := find(node["children"], domain_id):
            return found
    return {}


@pytest.mark.db
def test_api_a_arvore_com_contagens(api: TestClient) -> None:
    reply = api.get("/api/domains")
    assert reply.status_code == 200
    roots = reply.json()["domains"]
    assert FALLBACK_ID in {r["id"] for r in roots}
    transportes = find(roots, "transportes")
    auto = find(roots, AUTO)
    assert transportes["names"] == {"pt": "Transportes", "en": "Transport"}
    assert next(c["id"] for c in transportes["children"]) == AUTO
    assert (auto["documents"], auto["documents_total"]) == (2, 2)
    assert (transportes["documents"], transportes["documents_total"]) == (0, 2)
    assert find(roots, FALLBACK_ID)["documents"] == 1
    assert find(roots, FALLBACK_ID)["needs_review"] == 1
    assert auto["descriptions"]["pt"]
    assert auto["active"]


@pytest.mark.db
def test_api_atribuicao_humana(api: TestClient, db: sessionmaker[Session]) -> None:
    document = "zim:cd3wd/AUTO.HTM"  # tem '/' e '.': o caminho aceita-os
    reply = api.put(
        f"/api/domain-assignments/{document}", json={"domain_id": AUTO, "note": "manual de carros"}
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert (body["document_id"], body["domain_id"], body["method"]) == (document, AUTO, "human")
    assert body["confidence"] == 1.0
    assert not body["needs_review"]
    assert body["signals"]["note"] == "manual de carros"

    with db() as s:
        row = s.get(DomainAssignment, document)
        assert row is not None
        assert (row.domain_id, row.method) == (AUTO, "human")
        assert s.scalar(
            select(func.count()).select_from(ToolCall).where(ToolCall.tool == "domain_assign")
        )
        # …e uma passagem automática não lhe toca.
        again = place(
            s,
            document_id=document,
            source_name="cd3wdproject.org_en_all",
            embedder_model_id=MODEL,
            structure_hint="agricultura/culturas",
        )
        assert again.method == "human"

    roots = api.get("/api/domains").json()["domains"]
    assert find(roots, AUTO)["documents"] == 3
    assert find(roots, FALLBACK_ID)["documents"] == 0


@pytest.mark.db
def test_api_atribuicao_com_erros(api: TestClient) -> None:
    missing_domain = api.put(
        "/api/domain-assignments/zim:cd3wd/AUTO.HTM", json={"domain_id": "nao/existe"}
    )
    assert missing_domain.status_code == 404
    missing_doc = api.put("/api/domain-assignments/zim:nao/existe", json={"domain_id": AUTO})
    assert missing_doc.status_code == 404
    assert "Document" in missing_doc.json()["detail"]
    assert api.put("/api/domain-assignments/zim:cd3wd/AUTO.HTM", json={}).status_code == 422


@pytest.mark.db
def test_api_exige_um_principal_user(api: TestClient) -> None:
    no_key = TestClient(api.app)
    assert no_key.get("/api/domains").status_code == 401
    assert (
        no_key.put(
            "/api/domain-assignments/zim:cd3wd/AUTO.HTM", json={"domain_id": AUTO}
        ).status_code
        == 401
    )
    agent = {"Authorization": f"Bearer {api.agent_key}"}  # type: ignore[attr-defined]
    assert api.get("/api/domains", headers=agent).status_code == 403
    assert (
        api.put(
            "/api/domain-assignments/zim:cd3wd/AUTO.HTM", json={"domain_id": AUTO}, headers=agent
        ).status_code
        == 403
    )


def test_config_e_imutavel() -> None:
    config = load_config()
    assert isinstance(config, DomainConfig)
    assert load_config() is config  # em cache


# --- a decisão vetorial (sem base de dados) ------------------------------------------------------


def scored(top: float, second: float, rest: float, spread: float, n: int = 100) -> _Scored:
    """Um resultado em que as `n` folhas têm cosseno `rest` +- `spread` (o desvio-padrão)."""
    rng = np.random.default_rng(7)
    others = rest + spread * rng.standard_normal(n - 2)
    everything = np.concatenate([[top, second], others])
    ranked = [("a", top), ("b", second)]
    return _Scored(ranked, float(everything.mean()), float(everything.std()))


def test_um_vencedor_que_se_destaca_nao_precisa_de_revisao() -> None:
    result = scored(top=0.88, second=0.84, rest=0.78, spread=0.02)
    assert result.z > 3
    assert not result.weak
    assert not result.hopeless


def test_cosseno_alto_sem_destaque_e_fraco_como_o_lixo_da_amostra() -> None:
    """Texto sem sentido dá cosseno 0,85 com tudo (e5 é comprimido): o z é que o denuncia."""
    result = scored(top=0.855, second=0.80, rest=0.80, spread=0.025)
    assert result.top1 > 0.8
    assert result.margin > 0.02
    assert result.z < 3
    assert result.weak


def test_margem_curta_e_fraca_mesmo_com_z_alto() -> None:
    assert scored(top=0.90, second=0.898, rest=0.78, spread=0.02).weak


def test_sem_destaque_nenhum_nao_serve() -> None:
    assert scored(top=0.80, second=0.79, rest=0.78, spread=0.02).hopeless
    assert scored(top=0.55, second=0.2, rest=0.0, spread=0.1).hopeless  # cosseno absurdo


@pytest.mark.db
def test_os_chunks_valem_mais_do_que_o_vetor_de_artigo(tree: Session) -> None:
    ensure_vectors(tree, embedder_for(**{AUTO.replace("/", "__"): unit(1)}), MODEL)
    add_document(tree, "zim:wikipedia/dois")
    put_vector(tree, "zim:wikipedia/dois", unit(0, 1))  # o vetor de artigo não diz nada
    chunk = Chunk(document_id="zim:wikipedia/dois", ord=0, text="x")
    tree.add(chunk)
    tree.flush()
    tree.add(ChunkVector(chunk_id=chunk.id, model_id=MODEL, embedding=unit(1, 0.05)))
    tree.flush()
    result = place(
        tree,
        document_id="zim:wikipedia/dois",
        source_name="wikipedia_en_all",
        embedder_model_id=MODEL,
    )
    assert (result.domain_id, result.signals["vector"]["source"]) == (AUTO, "chunks")
    assert result.signals["vector"]["z"] > 3


# --- o voto das âncoras (kNN) e o atrator, sem base de dados -------------------------------------

DIM = 48
FILLERS = 30  # folhas de enchimento: com poucas folhas o z do melhor nunca chega a nada


def axis(*weights: float) -> np.ndarray:
    vector = np.zeros(DIM, dtype=np.float32)
    vector[: len(weights)] = weights
    return vector / np.linalg.norm(vector)


def cluster(center: np.ndarray, n: int, seed: int, noise: float = 0.1) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        v = center + noise * rng.standard_normal(DIM).astype(np.float32) / np.sqrt(DIM)
        out.append(v / np.linalg.norm(v))
    return out


def filler_vectors() -> dict[str, np.ndarray]:
    return {f"f/{i}": axis(*([0] * (6 + i)), 1) for i in range(FILLERS)}


def snapshot_with_anchors(
    anchors: list[tuple[str, np.ndarray]],
    *,
    leaf_vectors: dict[str, np.ndarray] | None = None,
    hub: dict[str, float] | None = None,
    anchor_hub: np.ndarray | None = None,
) -> domains.TreeSnapshot:
    vectors = leaf_vectors or {
        "a/x": axis(1), "a/y": axis(0.9, 0.4), "b/x": axis(0, 1), "c/x": axis(0, 0, 1),
        **filler_vectors(),
    }  # fmt: skip
    parents: dict[str, str | None] = {FALLBACK_ID: None, "f": None}
    for leaf in vectors:
        root = leaf.split("/")[0]
        parents[root] = None
        parents[leaf] = root
    return domains.TreeSnapshot(
        "m", parents, frozenset(vectors), vectors, {}, hub=hub or {},
        anchor_ids=tuple(f"d{i}" for i in range(len(anchors))),
        anchor_domains=tuple(d for d, _ in anchors),
        anchor_matrix=np.stack([v for _, v in anchors]) if anchors else None,
        anchor_hub=anchor_hub,
    )  # fmt: skip


def three_clusters() -> list[tuple[str, np.ndarray]]:
    return (
        [("a/x", v) for v in cluster(axis(1), 6, 1)]
        + [("a/y", v) for v in cluster(axis(1), 4, 2)]
        + [("b/x", v) for v in cluster(axis(0, 1), 25, 3)]
        + [("c/x", v) for v in cluster(axis(0, 0, 1), 25, 4)]
    )


def test_o_voto_dos_vizinhos_escolhe_o_dominio_de_topo_das_ancoras_proximas() -> None:
    snapshot = snapshot_with_anchors(three_clusters())

    vote = domains.knn_vote(axis(1, 0.05), snapshot)

    assert vote is not None and vote.top == "a"  # as âncoras a/x e a/y votam no mesmo topo
    assert vote.share > 0.95 and vote.neighbors >= domains.KNN_MIN_NEIGHBORS
    assert vote.ranked[0][0] == "a" and vote.signals()["top"] == "a"


def test_sem_ancoras_proximas_ou_suficientes_o_voto_nao_decide() -> None:
    assert domains.knn_vote(axis(1), snapshot_with_anchors([])) is None
    assert domains.knn_vote(axis(1), snapshot_with_anchors(three_clusters()[:3])) is None
    # um documento longe de todas as âncoras (cosseno ~0): ninguém vota
    longe = axis(0.01, 0.01, 0.01, 1)
    assert domains.knn_vote(longe, snapshot_with_anchors(three_clusters())) is None


def test_o_voto_ignora_o_proprio_documento() -> None:
    anchors = three_clusters()
    snapshot = snapshot_with_anchors(anchors)

    with_self = domains.knn_vote(anchors[0][1], snapshot)
    without_self = domains.knn_vote(anchors[0][1], snapshot, exclude="d0")

    assert with_self is not None and without_self is not None
    assert with_self.neighbors == without_self.neighbors + 1  # d0 era o vizinho mais próximo


def test_uma_ancora_atrator_nao_atrai_tudo() -> None:
    """Âncoras que se parecem com todos os documentos (cosseno alto com tudo) ganhavam sempre;
    descontar a semelhança média de cada âncora a documentos ao acaso tira-lhes o voto."""
    hubs = [("c/x", v) for v in cluster(axis(0.6, 0.6, 0.6), 8, 6, noise=0.05)]
    anchors = hubs + [("b/x", v) for v in cluster(axis(0, 1), 50, 7)]
    anchors += [("a/x", v) for v in cluster(axis(1), 10, 8)]
    # documentos ao acaso, todos virados para onde as âncoras atrator estão
    sample = np.stack(cluster(axis(1, 1, 1), 50, 9, noise=0.5))
    matrix = np.stack([v for _, v in anchors])
    query = axis(1, 0.5, 0.5, 0.05)  # perto do grupo atrator, e do tema «a/x» sem ser nenhum

    raw = domains.knn_vote(query, snapshot_with_anchors(anchors))
    fixed = domains.knn_vote(
        query, snapshot_with_anchors(anchors, anchor_hub=(matrix @ sample.T).mean(axis=1))
    )

    assert raw is not None and raw.top == "c" and raw.share > 0.9
    assert fixed is not None and fixed.top == "a"  # o tema, já sem o atrator à frente


def test_o_atrator_das_folhas_deixa_de_ganhar_a_todos() -> None:
    query = axis(1)
    attractor = np.array([0.85, 0.527] + [0] * (DIM - 2), dtype=np.float32)
    specific = np.array([0.83, 0, 0.558] + [0] * (DIM - 3), dtype=np.float32)
    vectors = {"a/x": specific, "b/x": attractor, "c/x": axis(0, 0, 0, 1), **filler_vectors()}
    hub = {leaf: 0.1 for leaf in vectors} | {"a/x": 0.70, "b/x": 0.80}
    plain = snapshot_with_anchors([], leaf_vectors=vectors)
    corrected = snapshot_with_anchors([], leaf_vectors=vectors, hub=hub)

    before = domains.score_leaves(None, query, "m", snapshot=plain)
    after = domains.score_leaves(None, query, "m", snapshot=corrected)

    assert before is not None and before.ranked[0][0] == "b/x" and not before.corrected
    assert after is not None and after.ranked[0][0] == "a/x" and after.corrected
    assert after.raw_top1 == pytest.approx(0.83)  # o cosseno do vencedor, sem a correção
    assert after.signals("document_vector", "m")["hub_corrected"] is True


def decide(snapshot: domains.TreeSnapshot, query: np.ndarray) -> domains.Placement:
    return domains.decide_vectors(
        (query, "chunks"), "novo", "wikipedia_en_all", "m", None, snapshot
    )


def test_a_decisao_com_ancoras_que_concordam_aceita_sem_revisao() -> None:
    placement = decide(snapshot_with_anchors(three_clusters()), axis(1, 0.05))

    assert (placement.method, placement.needs_review) == ("vector", False)
    assert placement.domain_id in ("a/x", "a/y") and placement.signals["knn"]["top"] == "a"
    assert placement.signals["knn"]["descriptions_agree"] is True


def test_ancoras_boas_mas_contrariadas_pelas_descricoes_vao_para_revisao() -> None:
    # 7 âncoras dizem «a» e 4 dizem «b» (votação boa, não esmagadora); as descrições dizem «b».
    swapped = {"a/x": axis(0, 1), "b/x": axis(1), "c/x": axis(0, 0, 1), **filler_vectors()}
    anchors = [("a/x", v) for v in cluster(axis(1), 7, 11)]
    anchors += [("b/x", v) for v in cluster(axis(1), 4, 12)]
    anchors += [("c/x", v) for v in cluster(axis(0, 0, 1), 30, 13)]
    anchors += [("f/0", v) for v in cluster(axis(*([0] * 6), 1), 30, 14)]
    snapshot = snapshot_with_anchors(anchors, leaf_vectors=swapped)

    placement = decide(snapshot, axis(1, 0.02))

    knn = placement.signals["knn"]
    assert domains.KNN_ACCEPT_SHARE <= knn["share"] < domains.KNN_TRUST_SHARE
    assert knn["descriptions_agree"] is False
    assert placement.needs_review  # decidiu-se pelas descrições, mas em revisão


def test_sem_ancoras_a_decisao_e_a_das_descricoes() -> None:
    placement = decide(snapshot_with_anchors([]), axis(1))

    assert "knn" not in placement.signals
    assert (placement.domain_id, placement.method, placement.needs_review) == (
        "a/x",
        "vector",
        False,
    )


def test_candidatos_do_lote_juntam_o_voto_e_as_descricoes() -> None:
    snapshot = snapshot_with_anchors(three_clusters())

    found = domains.candidates((axis(1, 0.05), "chunks"), snapshot, "m", "novo", limit=4)

    assert len(found) == 4 and len(set(found)) == 4
    assert found[0] in ("a/x", "a/y")  # a folha do domínio votado pelas âncoras vem primeiro
    sem_vetor = domains.candidates(None, snapshot, "m", "novo")
    assert set(sem_vetor) == {"a", "b", "c", "f"}  # sem vetor, os domínios de topo


# --- as âncoras e o atrator lidos da base de dados -----------------------------------------------


def add_anchor(
    session: Session, document_id: str, domain_id: str, vector: list[float], method: str = "prior"
) -> None:
    add_document(session, document_id)
    put_vector(session, document_id, vector)
    session.add(
        DomainAssignment(
            document_id=document_id, domain_id=domain_id, method=method, confidence=0.9
        )
    )
    session.flush()


def wiggle(vector: list[float], i: int, size: float = 0.05) -> list[float]:
    """O vetor com um pequeno desvio num eixo ao fim (para as âncoras não serem iguais)."""
    out = np.asarray(vector, dtype=np.float32).copy()
    out[(10 + i) % EMBEDDING_DIM] += size
    return [float(x) for x in out / np.linalg.norm(out)]


@pytest.mark.db
def test_o_snapshot_carrega_so_as_ancoras_do_prior_e_de_pessoas(tree: Session) -> None:
    ensure_vectors(tree, embedder_for(**{AUTO.replace("/", "__"): unit(1)}), MODEL)
    add_anchor(tree, "zim:a/p1", AUTO, unit(1, 0.1))
    add_anchor(tree, "zim:a/p2", AUTO, unit(1, 0.2))
    add_anchor(tree, "zim:a/h1", RECEITAS, unit(0, 1), method="human")
    add_anchor(tree, "zim:a/v1", AUTO, unit(1, 0.3), method="vector")  # automático: não é exemplo
    add_anchor(tree, "zim:a/l1", AUTO, unit(1, 0.4), method="llm")  # nem o do LLM
    add_anchor(tree, "zim:a/f1", FALLBACK_ID, unit(1, 0.5), method="human")  # nem o por-classificar
    add_document(tree, "zim:a/sem-vetor")
    tree.add(DomainAssignment(document_id="zim:a/sem-vetor", domain_id=AUTO, method="prior"))
    tree.flush()

    snapshot = domains.load_snapshot(tree, MODEL)

    assert snapshot.anchor_ids == ("zim:a/h1", "zim:a/p1", "zim:a/p2")
    assert snapshot.anchor_domains == (RECEITAS, AUTO, AUTO)
    assert snapshot.anchor_matrix is not None and snapshot.anchor_matrix.shape == (3, EMBEDDING_DIM)
    assert np.linalg.norm(snapshot.anchor_matrix, axis=1) == pytest.approx(1.0, abs=1e-5)
    assert snapshot.hub == {} and snapshot.anchor_hub is None  # há poucos documentos para o atrator


@pytest.mark.db
def test_o_snapshot_limita_as_ancoras_por_dominio(
    tree: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(domains, "ANCHORS_PER_DOMAIN", 2)
    for i in range(5):
        add_anchor(tree, f"zim:a/p{i}", AUTO, wiggle(unit(1), i))
    add_anchor(tree, "zim:a/r", RECEITAS, unit(0, 1))

    snapshot = domains.load_snapshot(tree, MODEL)

    assert sorted(snapshot.anchor_domains) == sorted([AUTO, AUTO, RECEITAS])
    again = domains.load_snapshot(tree, MODEL)
    assert again.anchor_ids == snapshot.anchor_ids  # a escolha é estável (hash do id)


@pytest.mark.db
def test_o_atrator_mede_se_em_documentos_ao_acaso_e_so_com_documentos_que_cheguem(
    tree: Session,
) -> None:
    ensure_vectors(
        tree,
        embedder_for(**{AUTO.replace("/", "__"): unit(1), "alimentacao__receitas": unit(0, 1)}),
        MODEL,
    )
    add_anchor(tree, "zim:a/p1", AUTO, unit(1, 0.1))
    for i in range(domains.HUB_MIN_DOCS - 2):  # 28 + 1 âncora: ainda não chega
        add_document(tree, f"zim:w/d{i}")
        put_vector(tree, f"zim:w/d{i}", wiggle(unit(0, 1), i))
    assert domains.load_snapshot(tree, MODEL).hub == {}

    for i in range(10):
        add_document(tree, f"zim:w/e{i}")
        put_vector(tree, f"zim:w/e{i}", wiggle(unit(0, 1), i, 0.1))
    snapshot = domains.load_snapshot(tree, MODEL)

    # a maioria dos documentos está virada para o eixo 1: a folha das receitas é o atrator
    assert snapshot.hub[RECEITAS] > snapshot.hub[AUTO]
    assert snapshot.hub[RECEITAS] == pytest.approx(0.97, abs=0.05)
    assert snapshot.anchor_hub is not None and len(snapshot.anchor_hub) == 1


@pytest.mark.db
def test_colocar_com_ancoras_proximas_vota_e_regista_o_voto(tree: Session) -> None:
    ensure_vectors(
        tree,
        embedder_for(**{AUTO.replace("/", "__"): unit(1)}),
        MODEL,
    )
    for i in range(8):  # exemplos de automóveis, e muitos outros de outros temas
        add_anchor(tree, f"zim:a/auto{i}", AUTO, wiggle(unit(1), i))
    for i in range(30):
        add_anchor(tree, f"zim:a/rec{i}", RECEITAS, wiggle(unit(0, 1), i))
    for i in range(30):
        add_anchor(tree, f"zim:a/cons{i}", CONSERVACAO, wiggle(unit(0, 0, 1), i))
    add_document(tree, "zim:wikipedia/carro")
    put_vector(tree, "zim:wikipedia/carro", unit(1, 0.05, 0.02))

    result = place(
        tree,
        document_id="zim:wikipedia/carro",
        source_name="wikipedia_en_teste",
        embedder_model_id=MODEL,
    )

    assert result.method == "vector" and not result.needs_review
    assert result.domain_id == AUTO  # a folha de topo «transportes», escolhida pelas descrições
    knn = result.signals["knn"]
    assert knn["top"] == "transportes" and knn["share"] > 0.9 and knn["descriptions_agree"]
    assert "zim:wikipedia/carro" not in str(knn)  # o voto nunca conta o próprio documento
    stored = assignment(tree, "zim:wikipedia/carro")
    assert stored.signals["knn"]["top"] == "transportes"
