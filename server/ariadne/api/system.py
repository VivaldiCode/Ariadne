"""Configuração em vigor (só leitura) para o módulo «Operação» da app web: o orçamento diário e
os limiares. Alterá-los é editar o `.env` e reiniciar: a app mostra-os, não os escreve."""

from fastapi import APIRouter
from pydantic import BaseModel

from ariadne import router
from ariadne.api.deps import PrincipalDep, ServicesDep

system_router = APIRouter(tags=["operations"])


class ServiceFlags(BaseModel):
    wiki: bool  # Wiki.js configurado (URL e token)
    curator: bool  # curador LLM configurado
    translate: bool  # LibreTranslate configurado
    embedder: str  # onnx | llamacpp | http | fake
    moodle: bool = False  # Moodle configurado (URL e token)
    jobe: bool = False  # Jobe configurado (sandbox de código)
    calibre: bool = False  # Calibre configurado (URL e chave)
    kavita: bool = False  # Kavita configurado (URL e chave de API)


class ConfigView(BaseModel):
    embedder_model_id: str | None
    index_daily_budget: int
    heat_half_life_days: float
    neighbors_heat_threshold: float
    neighbors_max_per_doc: int
    link_tau: float
    link_tau_accept: float
    router_tau_high: float
    router_tau_low: float
    router_historical_rule: bool
    import_heat_threshold: float
    curation_sample_rate: float
    translate_queries: bool
    services: ServiceFlags


@system_router.get("/config")
def get_config(services: ServicesDep, principal: PrincipalDep) -> ConfigView:
    """The parameters in effect (server environment variables): read-only. Excludes URLs and
    keys."""
    s = services.settings
    return ConfigView(
        embedder_model_id=router.current_model_id(services),
        index_daily_budget=s.index_daily_budget,
        heat_half_life_days=s.heat_half_life_days,
        neighbors_heat_threshold=s.neighbors_heat_threshold,
        neighbors_max_per_doc=s.neighbors_max_per_doc,
        link_tau=s.link_tau,
        link_tau_accept=s.link_tau_accept,
        router_tau_high=s.router_tau_high,
        router_tau_low=s.router_tau_low,
        router_historical_rule=s.router_historical_rule,
        import_heat_threshold=s.import_heat_threshold,
        curation_sample_rate=s.curation_sample_rate,
        translate_queries=s.translate_queries,
        services=ServiceFlags(
            wiki=bool(s.wikijs_url and s.wikijs_token),
            curator=bool(s.curator_url),
            translate=bool(s.libretranslate_url),
            embedder=s.embedder,
            moodle=bool(s.moodle_url and s.moodle_token),
            jobe=bool(s.jobe_url),
            calibre=bool(s.calibre_url and s.calibre_api_key),
            kavita=bool(s.kavita_url and s.kavita_api_key),
        ),
    )
