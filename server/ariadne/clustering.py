"""K-means esférico em numpy, sem scikit-learn (ARCHITECTURE.md, secção 4.5: as salas).

Os vetores de artigo são normalizados e o parecido é o cosseno, por isso o centróide de um grupo é
a média normalizada e o grupo de um vetor é o centróide com maior produto interno. Tudo é
determinístico: a semente decide a inicialização (k-means++) e os desempates fazem-se pelo índice.
"""

import math

import numpy as np
from numpy.typing import NDArray

Matrix = NDArray[np.float32]

BLOCK = 20_000  # linhas multiplicadas pelos centróides de cada vez (limita a memória)


def normalize(vectors: Matrix) -> Matrix:
    """Linhas com norma 1 (as de norma 0 ficam a zeros)."""
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return np.asarray(vectors / np.where(norms == 0, 1.0, norms), dtype=np.float32)


def room_count(documents: int, *, lowest: int, highest: int) -> int:
    """Quantas salas para `documents` artigos: ~√(n/2), entre `lowest` e `highest`, e nunca mais
    do que metade dos artigos (uma sala de um só artigo não agrupa nada)."""
    wanted = max(lowest, min(highest, round(math.sqrt(documents / 2))))
    return max(1, min(wanted, documents // 2))


def nearest(vectors: Matrix, centroids: Matrix) -> tuple[NDArray[np.intp], NDArray[np.float32]]:
    """Para cada vetor, o índice do centróide mais parecido (o menor, num empate) e a semelhança."""
    best = np.empty(len(vectors), dtype=np.intp)
    sims = np.empty(len(vectors), dtype=np.float32)
    for start in range(0, len(vectors), BLOCK):
        scores = vectors[start : start + BLOCK] @ centroids.T
        chosen = np.argmax(scores, axis=1)
        best[start : start + BLOCK] = chosen
        sims[start : start + BLOCK] = scores[np.arange(len(chosen)), chosen]
    return best, sims


def _seed_centroids(vectors: Matrix, k: int, rng: np.random.Generator) -> Matrix:
    """k-means++: o 1.º ao acaso, cada seguinte com probabilidade ∝ (1 − semelhança máxima)²."""
    n = len(vectors)
    chosen = [int(rng.integers(n))]
    closest = vectors @ vectors[chosen[0]]  # a maior semelhança a um centróide já escolhido
    for _ in range(1, k):
        weights = np.square(np.clip(1.0 - closest, 0.0, None)).astype(np.float64)
        weights[chosen] = 0.0
        total = weights.sum()
        if total <= 0:  # o que resta é tudo igual aos já escolhidos: escolhe-se o 1.º livre
            free = np.setdiff1d(np.arange(n), chosen)
            pick = int(free[0])
        else:
            pick = int(rng.choice(n, p=weights / total))
        chosen.append(pick)
        closest = np.maximum(closest, vectors @ vectors[pick])
    return vectors[chosen].copy()


def spherical_kmeans(
    vectors: Matrix, k: int, *, seed: int, iterations: int = 30
) -> tuple[Matrix, NDArray[np.intp]]:
    """Agrupa `vectors` (normalizados) em `k` grupos. Devolve os centróides (k × d, normalizados) e
    o grupo de cada vetor. O mesmo input e a mesma semente dão sempre o mesmo resultado."""
    n = len(vectors)
    if n == 0 or k < 1:
        raise ValueError("k-means needs at least one vector and one cluster")
    k = min(k, n)
    rng = np.random.default_rng(seed)
    centroids = _seed_centroids(vectors, k, rng)
    labels = np.full(n, -1, dtype=np.intp)
    for _ in range(iterations):
        new_labels, sims = nearest(vectors, centroids)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for group in range(k):
            members = labels == group
            if not members.any():  # grupo vazio: fica com o vetor pior servido pelos outros
                worst = int(np.argmin(sims))
                labels[worst] = group
                sims[worst] = 1.0
                members = labels == group
            centroids[group] = vectors[members].sum(axis=0)
        centroids = normalize(centroids)
    labels, _ = nearest(vectors, centroids)
    return drop_empty(centroids, labels)


def drop_empty(centroids: Matrix, labels: NDArray[np.intp]) -> tuple[Matrix, NDArray[np.intp]]:
    """Tira os centróides a que nenhum vetor ficou (a atribuição final pode esvaziar um grupo,
    por exemplo com vetores repetidos) e renumera os grupos: nunca há uma sala sem membros."""
    used = np.bincount(labels, minlength=len(centroids)) > 0
    if used.all():
        return centroids, labels
    renumber = np.cumsum(used) - 1
    return centroids[used].copy(), renumber[labels].astype(np.intp)


def match_centroids(
    old: Matrix, new: Matrix, *, minimum: float, center: Matrix | None = None
) -> dict[int, int]:
    """Emparelha cada centróide novo com, no máximo, um antigo (o mais parecido, se a semelhança
    chega a `minimum`): é como uma sala recalculada herda o id da anterior. Guloso, do par mais
    parecido para o menos; devolve índice novo -> índice antigo.

    `center` (a média dos vetores da ala) tira-se antes de comparar: os vetores do e5 partilham uma
    componente comum enorme e, sem isto, os centróides de salas **diferentes** da mesma ala têm
    cosseno 0,85–0,99 (ver a calibração em ARCHITECTURE.md, 4.5)."""
    if len(old) == 0 or len(new) == 0:
        return {}
    if center is not None:
        old, new = normalize(old - center), normalize(new - center)
    sims = new @ old.T
    order = np.dstack(np.unravel_index(np.argsort(-sims, axis=None, kind="stable"), sims.shape))[0]
    taken_new: set[int] = set()
    taken_old: set[int] = set()
    pairs: dict[int, int] = {}
    for i, j in order:
        if sims[i, j] < minimum:
            break
        if int(i) in taken_new or int(j) in taken_old:
            continue
        taken_new.add(int(i))
        taken_old.add(int(j))
        pairs[int(i)] = int(j)
    return pairs
