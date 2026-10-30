"""Os colaboradores partilhados pela API REST e pelo MCP (um só conjunto por processo)."""

import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from ariadne.config import Settings
from ariadne.curation.curator import Curator
from ariadne.curation.monitor import CuratorMonitor, CuratorStatus
from ariadne.db.session import make_engine
from ariadne.embedder import Embedder, EmbedderError, get_embedder
from ariadne.kiwix import KiwixClient
from ariadne.mempalace_store import make_store
from ariadne.semantic import PgVectorStore, SemanticStore
from ariadne.sources.registry import SourceRegistry
from ariadne.translate import Translator
from ariadne.translate.libretranslate import LibreTranslate
from ariadne.wiki.base import WikiBackend
from ariadne.wiki.wikijs import WikiJsBackend

if TYPE_CHECKING:
    from ariadne.books.backend import LibraryBackend
    from ariadne.books.kavita import KavitaClient
    from ariadne.compute.backends import ComputeBackend
    from ariadne.translate.service import TranslationService
    from ariadne.vault.config import IntegrationConfig
    from ariadne.zimcatalog.opds import CatalogClient

logger = logging.getLogger(__name__)

# Que campos de `Settings` alimentam cada cliente em cache: quando o cofre muda um deles, o cliente
# refaz-se (sem reiniciar o processo). Ver `Services.sync_config`.
CLIENT_FIELDS: dict[str, tuple[str, ...]] = {
    "wiki": ("wikijs_url", "wikijs_token", "wikijs_locale"),
    "library": ("calibre_url", "calibre_api_key", "calibre_timeout_seconds"),
    "kavita": ("kavita_url", "kavita_api_key", "kavita_timeout_seconds"),
    "curator": ("curator_url", "curator_model", "curator_api_key", "curator_timeout_seconds"),
    "translator": ("libretranslate_url", "libretranslate_api_key", "translate_timeout_seconds"),
}


@dataclass
class Services:
    # A configuração efetiva: as variáveis de ambiente com o cofre das integrações por cima
    # (`ariadne.vault`). `sync_config` troca-a quando o cofre muda; quem a guarda numa variável
    # local vê um instantâneo, o que chega para uma tarefa.
    settings: Settings
    sessions: sessionmaker[Session]
    registry: SourceRegistry
    kiwix: KiwixClient
    engine: Engine | None = None  # só quando foi `Services.open` a criá-lo
    embedder: Embedder | None = None  # por omissão, o de `get_embedder(settings)`
    store: SemanticStore = field(default_factory=PgVectorStore)
    store_name: str = "pgvector"  # o armazenamento que está mesmo a funcionar (SEMANTIC_STORE)
    store_note: str | None = None  # porque não é o pedido, se não for
    wiki: WikiBackend | None = None  # por omissão, o Wiki.js de `settings` (se estiver configurado)
    curator: Curator | None = None  # idem, o de `settings.curator_url`
    translator: Translator | None = None  # idem, o LibreTranslate de `settings.libretranslate_url`
    library: "LibraryBackend | None" = None  # idem, o Calibre de `settings.calibre_url`
    kavita: "KavitaClient | None" = None  # idem, o Kavita de `settings.kavita_url`
    catalog: "CatalogClient | None" = None  # idem, o catálogo OPDS do Kiwix
    _translation: "TranslationService | None" = None
    # Os destinos de computação (local, Kaggle, Colab, …); por omissão os de `settings`.
    backends: "dict[str, ComputeBackend] | None" = None
    # O HEAD do repositório de cursos da última sincronização sem erros: o worker `courses` não
    # relê nada enquanto o repositório não muda.
    courses_head: str | None = None
    # Caches do importador do Wiki.js, só deste processo: o lixo de cada fonte, a árvore de domínios
    # carregada, os resumos das páginas de domínio já escritas… (ver `workers.importer`).
    wiki_cache: dict[str, Any] = field(default_factory=dict)
    # O cofre das integrações; `None` (testes, ou sem base de dados) = só valem as `settings`.
    config: "IntegrationConfig | None" = None
    # Se o curador (o Mac) está a responder: o worker não reclama `curate_page` enquanto não.
    curator_monitor: CuratorMonitor = field(default_factory=CuratorMonitor, repr=False)
    _config_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def sync_config(self) -> bool:
        """Relê o cofre (se a cache curta expirou) e, se mudou, passa a usar os valores novos:
        as `settings` efetivas e os clientes das integrações afetadas refazem-se. Barato quando
        nada mudou; chamam-no os `get_*`, cada ciclo do worker e cada pedido da API."""
        if self.config is None:
            return False
        effective = self.config.effective()
        if effective is self.settings:
            return False
        with self._config_lock:
            old = self.settings
            if effective is old:
                return False
            self.settings = effective
            changed = {
                name
                for name, keys in CLIENT_FIELDS.items()
                if any(getattr(old, k) != getattr(effective, k) for k in keys)
            }
            # Não se fecham os clientes antigos: outra thread pode estar a meio de um pedido.
            if "wiki" in changed:
                self.wiki = None
                self.wiki_cache.clear()
            if "library" in changed:
                self.library = None
            if "kavita" in changed:
                self.kavita = None
            if "curator" in changed:
                self.curator = None
            if "translator" in changed:
                self.translator = None
                self._translation = None
            self.backends = None  # os destinos leem as credenciais de `settings`
            logger.info("Integrations configuration updated (%s)", ", ".join(sorted(changed)))
        return True

    def get_wiki(self) -> WikiBackend | None:
        """O nível 2, ou `None` se o Wiki.js não está configurado (URL e token): o resto degrada."""
        self.sync_config()
        if self.wiki is None and self.settings.wikijs_url and self.settings.wikijs_token:
            self.wiki = WikiJsBackend(
                self.settings.wikijs_url,
                self.settings.wikijs_token,
                locale=self.settings.wikijs_locale,
            )
        return self.wiki

    def get_library(self) -> "LibraryBackend":
        """A biblioteca de livros (Calibre). Sem `CALIBRE_URL` levanta `LibraryUnavailable` com
        uma mensagem clara; quem chama mostra-a."""
        self.sync_config()
        if self.library is None:
            from ariadne.books.calibre import CalibreBackend

            self.library = CalibreBackend(
                self.settings.calibre_url,
                api_key=self.settings.calibre_api_key,
                timeout=self.settings.calibre_timeout_seconds,
            )
        return self.library

    def get_kavita(self) -> "KavitaClient | None":
        """O Kavita, ou `None` sem `KAVITA_URL` e `KAVITA_API_KEY` (o resto funciona na mesma:
        os resultados é que ficam sem o URL do leitor)."""
        self.sync_config()
        if self.kavita is None and self.settings.kavita_url and self.settings.kavita_api_key:
            from ariadne.books.kavita import KavitaClient

            self.kavita = KavitaClient(
                self.settings.kavita_url,
                self.settings.kavita_api_key,
                timeout=self.settings.kavita_timeout_seconds,
            )
        return self.kavita

    def get_catalog(self) -> "CatalogClient":
        """O catálogo OPDS do Kiwix (`KIWIX_CATALOG_URL`), com cache curta."""
        if self.catalog is None:
            from ariadne.zimcatalog.opds import CatalogClient

            self.catalog = CatalogClient(
                self.settings.kiwix_catalog_url,
                ttl=self.settings.kiwix_catalog_ttl_seconds,
                timeout=self.settings.kiwix_catalog_timeout_seconds,
            )
        return self.catalog

    def get_curator(self) -> Curator | None:
        """O curador, ou `None` sem `CURATOR_URL`: o que precisa de curadoria fica à espera."""
        self.sync_config()
        if self.curator is None and self.settings.curator_url:
            self.curator = Curator(
                self.settings.curator_url,
                self.settings.curator_model,
                api_key=self.settings.curator_api_key,
                timeout=self.settings.curator_timeout_seconds,
            )
        return self.curator

    def curator_status(self, *, force: bool = False) -> CuratorStatus:
        """O curador está a responder? (cache curta; `online=None` sem `CURATOR_URL`)."""
        self.sync_config()
        return self.curator_monitor.status(self.settings.curator_url, force=force)

    def curation_enabled(self) -> bool:
        """Há alguém que possa curar? O curador local (`CURATOR_URL`) ou um destino de lote
        externo configurado (Kaggle). O importador usa-o para decidir se enfileira `curate_page`:
        com só a GPU externa, o job existe na mesma e o painel diz quem o consome."""
        if self.get_curator() is not None:
            return True
        return any(b.mode == "auto" and b.configured() for b in self.get_backends().values())

    def get_backends(self) -> "dict[str, ComputeBackend]":
        """Os destinos de computação por id (`ariadne.compute.backends`)."""
        self.sync_config()
        if self.backends is None:
            from ariadne.compute.backends import build_backends  # evita um import circular

            self.backends = build_backends(self.settings)
        return self.backends

    def get_translation(self) -> "TranslationService":
        """A tradução; sem `LIBRETRANSLATE_URL` (nem tradutor injetado) fica desligada e devolve
        sempre o texto como veio, com um aviso."""
        self.sync_config()
        if self._translation is None:
            from ariadne.translate.service import TranslationService  # evita um import circular

            settings = self.settings
            if self.translator is None and settings.libretranslate_url:
                self.translator = LibreTranslate(
                    settings.libretranslate_url,
                    api_key=settings.libretranslate_api_key,
                    timeout=settings.translate_timeout_seconds,
                )
            self._translation = TranslationService(self.translator, self.sessions, settings)
        return self._translation

    def get_embedder(self) -> Embedder:
        """O embedder do processo (carrega o modelo à primeira chamada). Pode falhar com
        `EmbedderError`: quem chama degrada para o nível 1."""
        return self.embedder or get_embedder(self.settings)

    @classmethod
    def open(cls, settings: Settings) -> "Services":
        """Liga à base de dados, ao kiwix e à pasta de ZIM; `close()` liberta tudo."""
        from ariadne.vault.config import IntegrationConfig

        engine = make_engine(settings.database_url)
        sessions = sessionmaker(engine)
        config = IntegrationConfig(settings, sessions)
        effective = config.effective()  # o cofre das integrações por cima do ambiente
        choice = make_store(effective)
        return cls(
            settings=effective,
            sessions=sessions,
            registry=SourceRegistry.from_settings(effective),
            kiwix=KiwixClient(effective.kiwix_url),
            engine=engine,
            store=choice.store,
            store_name=choice.name,
            store_note=choice.note,
            config=config,
        )

    def warm_embedder(self) -> None:
        """Descarrega e carrega o modelo já, para o 1.º `search` não pagar isso. Corre em
        segundo plano no arranque do servidor; se falhar, o `search` degrada para o nível 1."""
        try:
            embedder = self.get_embedder()
            embedder.embed_passages(["aquecimento"])  # só as passagens descarregam o modelo
            logger.info("Embedder ready: %s", embedder.model_id)
        except EmbedderError as exc:
            logger.warning("Embedder unavailable at startup (level 3 disabled): %s", exc)

    def sync_sources(self) -> None:
        """Abre os ZIM de ZIM_DIR e regista-os em `source`; sem base de dados o arranque segue."""
        try:
            with self.sessions() as session:
                report = self.registry.sync(session)
        except Exception:
            logger.exception("Could not sync the sources at startup")
            return
        for file, reason in report.errors.items():
            logger.warning("ZIM ignored: %s (%s)", file, reason)

    def close(self) -> None:
        self.registry.close()
        self.kiwix.close()
        if self.wiki is not None:
            self.wiki.close()
        if self.curator is not None:
            self.curator.close()
        if self.translator is not None:
            self.translator.close()
        if self.library is not None:
            self.library.close()
        if self.kavita is not None:
            self.kavita.close()
        if self.catalog is not None:
            self.catalog.close()
        if self.engine is not None:
            self.engine.dispose()
