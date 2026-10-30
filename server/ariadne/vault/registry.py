"""O que o cofre gere: as integrações, e para cada uma os campos (segredos e opções).

Cada campo é um atributo de `Settings` (`wikijs_token` ↔ variável `WIKIJS_TOKEN`). O valor do cofre
ganha ao da variável de ambiente; sem cofre, vale a variável. Ficam de fora o que tem de existir
antes de se chegar à base de dados (`DATABASE_URL`, `ARIADNE_MASTER_KEY`) e os parâmetros de
afinação (limiares, intervalos), que não são integrações.

Para acrescentar uma integração: um `Integration` aqui e, se o teste de ligação for útil, uma
função em `testers.py` registada em `TESTERS`.
"""

from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["secret", "url", "text", "number", "list"]


@dataclass(frozen=True)
class Field:
    key: str  # atributo de Settings
    label: str
    kind: Kind = "text"
    help: str = ""
    # Segredo que o Ariadne inventa (a chave do Calibre, a do Jobe): o dashboard pode gerá-lo.
    generable: bool = False
    # O outro lado que precisa do mesmo valor (ex. «NAS»); só informativo, mostra-se no ecrã.
    shared_with: str = ""

    @property
    def env(self) -> str:
        return self.key.upper()

    @property
    def secret(self) -> bool:
        return self.kind == "secret"

    @property
    def required(self) -> bool:
        return self.key in REQUIRED


@dataclass(frozen=True)
class Integration:
    id: str
    label: str
    description: str
    fields: tuple[Field, ...] = field(default_factory=tuple)


# O mínimo para a integração funcionar (o resto são opções com omissão). O Kaggle pede o
# utilizador e UM de `kaggle_api_token`/`kaggle_key`: ver `admin.configured`.
REQUIRED = frozenset(
    {
        "wikijs_url",
        "wikijs_token",
        "kiwix_url",
        "calibre_url",
        "calibre_api_key",
        "kavita_url",
        "kavita_api_key",
        "moodle_url",
        "moodle_token",
        "jobe_url",
        "jobe_api_key",
        "curator_url",
        "kaggle_username",
        "libretranslate_url",
        "embedder_url",
        "release_github_repo",
        "release_github_token",
        "mempalace_dsn",
    }
)

INTEGRATIONS: tuple[Integration, ...] = (
    Integration(
        "wikijs",
        "Wiki.js",
        "Level 2: where Ariadne publishes the curated pages.",
        (
            Field("wikijs_url", "Internal URL", "url"),
            Field("wikijs_token", "API key", "secret", "Administration → API Access."),
            Field("wikijs_public_url", "Public URL", "url", "As the user sees it."),
            Field("wikijs_locale", "Default locale"),
            Field("wikijs_locales", "Publishing locales", "list", "Comma-separated."),
        ),
    ),
    Integration(
        "kiwix",
        "Kiwix",
        "The kiwix-serve that serves the ZIMs.",
        (
            Field("kiwix_url", "Internal URL", "url"),
            Field("kiwix_public_url", "Public URL", "url"),
        ),
    ),
    Integration(
        "calibre",
        "Calibre",
        "Book library management (HTTP agent on the NAS).",
        (
            Field("calibre_url", "Agent URL", "url"),
            Field(
                "calibre_api_key",
                "API key",
                "secret",
                "The same one the Calibre agent reads on the NAS (calibre.env).",
                generable=True,
                shared_with="NAS (calibre.env)",
            ),
        ),
    ),
    Integration(
        "kavita",
        "Kavita",
        "The book reader.",
        (
            Field("kavita_url", "Internal URL", "url"),
            Field(
                "kavita_api_key", "API key", "secret", "`ariadne kavita bootstrap` generates it."
            ),
            Field("kavita_public_url", "Public URL", "url"),
            Field("kavita_library_id", "Library ID", "number"),
        ),
    ),
    Integration(
        "moodle",
        "Moodle",
        "Where the courses are published.",
        (
            Field("moodle_url", "Internal URL", "url"),
            Field("moodle_token", "Web service token", "secret", "`ariadne moodle bootstrap`."),
            Field("moodle_public_url", "Public URL", "url"),
        ),
    ),
    Integration(
        "jobe",
        "Jobe (code sandbox)",
        "Runs the exercise code; Ariadne never runs it on the server.",
        (
            Field("jobe_url", "Gateway URL", "url"),
            Field(
                "jobe_api_key",
                "API key",
                "secret",
                "The same one Moodle uses to call the gateway (JOBE_APIKEY on the NAS).",
                generable=True,
                shared_with="NAS (moodle.env)",
            ),
        ),
    ),
    Integration(
        "curator",
        "Curator (Mac)",
        "The LLM that rewrites and validates articles: llama-server on the Mac (OpenAI API).",
        (
            Field("curator_url", "Server URL", "url", "http://<Mac IP>:8085"),
            Field("curator_model", "Modelo", "text", "The name the server announces."),
            Field("curator_api_key", "API key", "secret", "llama-server's --api-key."),
        ),
    ),
    Integration(
        "kaggle",
        "Kaggle (external GPU)",
        "The batch on Kaggle's free GPU.",
        (
            Field("kaggle_username", "User", "text"),
            Field("kaggle_api_token", "API token", "secret", "kaggle.com/settings."),
            Field("kaggle_key", "Legacy key", "secret", "Only if there is no token."),
            Field("kaggle_model", "Notebook model", "text"),
            Field("kaggle_accelerator", "Accelerator", "text"),
        ),
    ),
    Integration(
        "libretranslate",
        "LibreTranslate",
        "PT⇄EN translation.",
        (
            Field("libretranslate_url", "URL", "url"),
            Field("libretranslate_api_key", "API key", "secret"),
            Field("libretranslate_pt_code", "Portuguese code", "text", "pt or pt-PT."),
        ),
    ),
    Integration(
        "embedder",
        "Embeddings (HTTP)",
        "Only with EMBEDDER=http|llamacpp: the /v1/embeddings endpoint.",
        (Field("embedder_url", "URL", "url"),),
    ),
    Integration(
        "release",
        "Releases (GitHub)",
        "The weekly publication of the open release.",
        (
            Field("release_github_repo", "Repository", "text", "owner/repository."),
            Field("release_github_token", "GitHub token", "secret"),
        ),
    ),
    Integration(
        "mempalace",
        "MemPalace",
        "The optional mirror to MemPalace.",
        (Field("mempalace_dsn", "Postgres DSN", "secret", "Carries the database password."),),
    ),
    Integration(
        "privacidade",
        "Privacy and network",
        "Which machines may receive text of the private class (SSRF protection).",
        (
            Field(
                "private_allowed_hosts",
                "Allowed hosts",
                "list",
                "Comma-separated (the Mac's IP, `*.home`…). Empty: the compose services.",
            ),
        ),
    ),
)

FIELDS: dict[str, Field] = {f.key: f for i in INTEGRATIONS for f in i.fields}
OWNER: dict[str, str] = {f.key: i.id for i in INTEGRATIONS for f in i.fields}
BY_ID: dict[str, Integration] = {i.id: i for i in INTEGRATIONS}
