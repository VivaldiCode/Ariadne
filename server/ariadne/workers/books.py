"""Worker `books`: mantém o índice dos livros em dia.

Tarefa periódica: extrai e indexa os livros da tabela `book` que ainda não foram extraídos (ou
cujo extrator mudou) e, se o Kavita está configurado, descobre onde cada livro ficou no leitor
(para o URL do leitor nos resultados), pedindo-lhe a leitura da biblioteca quando faltam livros.
Um Kavita em baixo não derruba nada: os resultados ficam sem o URL do leitor.
"""

import logging
import threading

from ariadne.books.index import BookSyncReport, sync_books
from ariadne.books.kavita import KavitaError, sync_locations
from ariadne.services import Services

logger = logging.getLogger(__name__)


def run_books(services: Services, stop: threading.Event | None = None) -> BookSyncReport:
    report = sync_books(services)
    if report.books or report.errors:
        logger.info(
            "Books: %d extracted, %d chapters (%d flagged), %d errors",
            report.books,
            report.chapters,
            report.held,
            len(report.errors),
        )
    kavita = services.get_kavita()
    if kavita is not None and services.settings.kavita_library_id:
        try:
            with services.sessions() as session:
                located = sync_locations(
                    session,
                    kavita,
                    services.settings.kavita_library_id,
                    services.settings.kavita_books_path,
                )
            if located.mapped:
                logger.info("Kavita: %d books located in the reader", located.mapped)
        except KavitaError as exc:
            logger.warning("Kavita: %s", exc)
    return report
