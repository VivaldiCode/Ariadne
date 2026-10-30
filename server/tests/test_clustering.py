"""K-means esférico: grupos separados, determinismo e herança dos ids das salas."""

import numpy as np
import pytest

from ariadne.clustering import (
    drop_empty,
    match_centroids,
    nearest,
    normalize,
    room_count,
    spherical_kmeans,
)


def three_blobs(per_group: int = 20, seed: int = 1) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    axes = np.eye(16, dtype=np.float32)[:3]
    vectors = np.concatenate(
        [axes[g] + 0.05 * rng.standard_normal((per_group, 16)).astype(np.float32) for g in range(3)]
    )
    truth = np.repeat(np.arange(3), per_group)
    return normalize(vectors.astype(np.float32)), truth


def test_grupos_separados_ficam_cada_um_na_sua_sala() -> None:
    vectors, truth = three_blobs()

    centroids, labels = spherical_kmeans(vectors, 3, seed=7)

    assert centroids.shape == (3, 16)
    assert np.allclose(np.linalg.norm(centroids, axis=1), 1.0, atol=1e-5)
    # Cada grupo verdadeiro cai inteiro numa só sala, e salas diferentes para grupos diferentes.
    rooms = [set(labels[truth == g].tolist()) for g in range(3)]
    assert all(len(r) == 1 for r in rooms)
    assert len({next(iter(r)) for r in rooms}) == 3


def test_mesma_semente_mesmo_resultado_e_outra_semente_pode_diferir() -> None:
    vectors, _ = three_blobs()

    first = spherical_kmeans(vectors, 3, seed=7)
    again = spherical_kmeans(vectors, 3, seed=7)

    assert np.array_equal(first[0], again[0])
    assert np.array_equal(first[1], again[1])


def test_vetores_repetidos_nao_partem_o_algoritmo() -> None:
    same = normalize(np.ones((10, 8), dtype=np.float32))

    centroids, labels = spherical_kmeans(same, 4, seed=1)

    assert centroids.shape[0] == 1  # todos iguais: só há um grupo (os vazios caem)
    assert labels.tolist() == [0] * 10


def test_k_maior_que_n_e_cortado_e_zero_vetores_e_erro() -> None:
    vectors, _ = three_blobs(per_group=1)
    centroids, _ = spherical_kmeans(vectors, 10, seed=1)
    assert len(centroids) == 3
    with pytest.raises(ValueError):
        spherical_kmeans(np.empty((0, 4), dtype=np.float32), 2, seed=1)


def test_nearest_desempata_pelo_menor_indice() -> None:
    centroids = normalize(np.array([[1, 0], [1, 0]], dtype=np.float32))
    best, sims = nearest(normalize(np.array([[1, 0]], dtype=np.float32)), centroids)
    assert best.tolist() == [0]
    assert sims[0] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("documents", "expected"),
    [
        (16, 8),  # √8 ≈ 3: sobe ao mínimo
        (200, 10),  # √100
        (2_000_000, 200),  # √1 000 000 = 1000: desce ao máximo
        (10, 5),  # nunca mais de metade dos artigos
        (1, 1),
    ],
)
def test_numero_de_salas(documents: int, expected: int) -> None:
    assert room_count(documents, lowest=8, highest=200) == expected


def test_match_herda_o_id_do_mais_parecido_e_so_acima_do_limiar() -> None:
    old = normalize(np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float32))
    new = normalize(np.array([[0.1, 1, 0], [1, 0.1, 0], [1, 1, 1]], dtype=np.float32))

    pairs = match_centroids(old, new, minimum=0.9)

    assert pairs == {0: 1, 1: 0}  # o terceiro não se parece com nenhum: sala nova
    assert match_centroids(old, new, minimum=0.999) == {}
    assert match_centroids(np.empty((0, 3), dtype=np.float32), new, minimum=0.5) == {}


def test_match_nao_da_a_mesma_sala_antiga_a_duas_novas() -> None:
    old = normalize(np.array([[1, 0]], dtype=np.float32))
    new = normalize(np.array([[1, 0.05], [1, 0.1]], dtype=np.float32))

    pairs = match_centroids(old, new, minimum=0.5)

    assert len(pairs) == 1
    assert pairs == {0: 0}  # a mais parecida (a 1.ª) fica com ela


def test_drop_empty_tira_os_centroides_sem_membros_e_renumera() -> None:
    centroids = normalize(np.eye(4, dtype=np.float32))
    labels = np.array([0, 2, 2, 3, 0], dtype=np.intp)  # o grupo 1 ficou vazio

    kept, relabeled = drop_empty(centroids, labels)

    assert kept.shape == (3, 4)
    assert np.array_equal(kept, centroids[[0, 2, 3]])
    assert relabeled.tolist() == [0, 1, 1, 2, 0]
    same, unchanged = drop_empty(centroids, np.array([0, 1, 2, 3], dtype=np.intp))
    assert same is centroids and unchanged.tolist() == [0, 1, 2, 3]


def test_kmeans_nunca_devolve_grupos_vazios() -> None:
    # 12 vetores iguais e 3 iguais entre si, pedindo 6 grupos: só há 2 grupos distintos.
    rows = [[1.0, 0.0]] * 12 + [[0.0, 1.0]] * 3
    vectors = normalize(np.array(rows, dtype=np.float32))

    centroids, labels = spherical_kmeans(vectors, 6, seed=3)

    assert len(centroids) == len(set(labels.tolist()))
    assert set(labels.tolist()) == set(range(len(centroids)))


def test_match_com_centro_tira_a_componente_comum_dos_vetores() -> None:
    # Como os vetores do e5: uma componente comum enorme (eixo 0) e pouco mais. Sem a retirar, os
    # três centróides têm cosseno > 0,99 entre si e qualquer um «se parece» com qualquer outro.
    common = np.array([10.0, 0.0, 0.0, 0.0], dtype=np.float32)
    topics = np.array([[0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=np.float32)
    old = normalize(common + topics)
    new = normalize(common + topics[[1, 0, 2]] + 0.05)  # o 1.º e o 2.º trocaram de ordem
    centre = old.mean(axis=0)

    assert match_centroids(old, new, minimum=0.7, center=centre) == {0: 1, 1: 0, 2: 2}
    other = normalize(common + np.array([[0, 0, 0, 1]], dtype=np.float32) * 3)  # outro tema
    assert match_centroids(old[:1], other, minimum=0.9) == {0: 0}  # sem o centro: «parecido»
    assert match_centroids(old[:1], other, minimum=0.7, center=centre) == {}  # com ele: não
