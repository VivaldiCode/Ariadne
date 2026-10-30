"""Worker `decay`: compacta o calor (ARCHITECTURE.md, secção 6.1) e faz as limpezas periódicas.

A leitura já aplica o decaimento em tempo real; isto só grava o valor decaído e põe
`heat_at = now()`, para os números guardados não ficarem velhos nem os cálculos crescerem.
"""

import logging

from ariadne import heat
from ariadne.retention import purge_client_queries
from ariadne.services import Services
from ariadne.translate.service import prune_cache

logger = logging.getLogger(__name__)


def run_decay(services: Services) -> tuple[int, int]:
    with services.sessions() as session:
        documents, chunks = heat.compact(session, services.settings.heat_half_life_days)
        session.commit()
    logger.info("Heat compacted: %d documents, %d chunks", documents, chunks)
    settings = services.settings
    with services.sessions() as session:  # a cache de traduções também envelhece
        pruned = prune_cache(
            session, settings.translate_cache_ttl_days, settings.translate_cache_max_rows
        )
        session.commit()
    if pruned:
        logger.info("Translation cache: %d entries deleted", pruned)
    with services.sessions() as session:  # e o texto das perguntas dos clientes externos
        queries, calls = purge_client_queries(session, settings.client_query_retention_days)
        session.commit()
    if queries or calls:
        logger.info("Client retention: %d searches and %d calls without text", queries, calls)
    return documents, chunks
