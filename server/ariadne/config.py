"""Configuração por variáveis de ambiente, com os mesmos nomes do docker-compose.yml."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings

from ariadne.models_policy import DEFAULT_BATCH_MODEL, assert_allowed_model
from ariadne.translate.protect import DEFAULT_TERMS


class Settings(BaseSettings):
    database_url: str
    kiwix_url: str = "http://localhost:8801"
    # URL do kiwix-serve como o utilizador o vê (nas respostas); kiwix_url é o interno.
    kiwix_public_url: str = "http://localhost:8801"
    # Livros por pedido de pesquisa ao kiwix-serve (por UUID): ~570 livros não cabem num só URL.
    kiwix_search_batch: int = 100
    # Wiki.js (nível 2, Fase 6). `wikijs_url` é o interno; sem token o nível 2 fica desligado.
    wikijs_url: str = ""
    wikijs_token: str = ""
    wikijs_public_url: str = "http://localhost:8802"  # como o utilizador o vê (nas respostas)
    # Só para `ariadne wiki bootstrap`: o administrador do Wiki.js (setup e chaves de API).
    wikijs_admin_email: str = ""
    wikijs_admin_password: str = ""
    # Locale do site Wiki.js: o das pesquisas e o de quem não diz qual. Cada página leva o da sua
    # língua (`/<locale>/<domínio>/…`, com o *namespacing* ligado).
    wikijs_locale: str = "en"
    # Os locales em que se publica, separados por vírgulas. Se for definido, manda; se não, o
    # importador pergunta ao Wiki.js (`localization.config`) e só usa este valor (`en,pt`) quando
    # ele não responde. Uma língua fora desta lista não se publica.
    wikijs_locales: str = "en,pt"
    zim_dir: Path = Path("/zim")
    # A aplicação web já compilada (`web/dist`); sem `index.html` nesta pasta não se monta.
    web_dir: Path = Path("/app/web")
    # `fake` é só para testes (vetores determinísticos a partir do texto).
    embedder: Literal["onnx", "llamacpp", "http", "fake"] = "onnx"
    embedder_model: str = "intfloat/multilingual-e5-small"
    # Revisão fixa do repositório do modelo (secção 9.4): trocá-la é trocar de modelo.
    embedder_revision: str = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
    embedder_variant: Literal["fp32", "int8"] = "fp32"
    embedder_threads: int = 0  # 0 = omissão do onnxruntime
    embedder_url: str = ""  # endpoint /v1/embeddings (llamacpp e http)
    embedder_model_id: str = ""  # obrigatório com llamacpp/http: o model_id dos vetores
    embedder_batch: int = 16  # textos por chamada ao modelo
    models_dir: Path = Path("/models")
    # Indexação (secções 6 e 10).
    chunk_words: int = 180  # ≈ 256 tokens
    index_daily_budget: int = 20_000  # chunks vetorizados por dia
    job_max_attempts: int = 5
    job_stale_minutes: int = 15  # um job 'running' há mais tempo é dado como preso
    worker_poll_seconds: float = 1.0
    decay_interval_hours: float = 6.0
    # Calor (secção 6.1).
    heat_half_life_days: float = 30.0
    # Vizinhos (secção 6.2 a 6.4).
    neighbors_heat_threshold: float = 5.0  # H_viz: calor a partir do qual se expande
    neighbors_max_per_doc: int = 50  # vizinhos novos por expansão (um salto só)
    neighbors_room_limit: int = 10  # da sala, só os mais quentes
    neighbors_semantic_k: int = 10  # vizinhos mais próximos em `document_vector`
    neighbors_batch: int = 20  # documentos expandidos por ronda
    neighbors_interval_seconds: float = 60.0
    # Pesos por tipo: a prioridade na fila é o calor da origem × o peso.
    neighbors_weight_adjacency: float = 1.0
    neighbors_weight_links: float = 0.6
    neighbors_weight_cross: float = 0.8
    neighbors_weight_room: float = 0.4
    neighbors_weight_semantic: float = 0.5
    # Salas (secção 4.5): k-means esférico sobre os vetores de artigo de cada ala. O nº de salas é
    # ~√(n/2), entre `rooms_min` e `rooms_max`; uma ala com menos de `rooms_min_documents` artigos
    # com vetor fica sem salas. Recalcula-se quando a ala cresce `rooms_growth` (fração).
    rooms_min: int = 8
    rooms_max: int = 200
    rooms_min_documents: int = 16
    rooms_growth: float = 0.25
    rooms_train_sample: int = 100_000  # vetores usados no k-means (os outros só se atribuem)
    rooms_seed: int = 9  # a mesma semente dá sempre as mesmas salas
    # A sala nova herda o id da antiga com o centróide mais parecido, se o cosseno (com a média da
    # ala retirada) chega a isto: calibrado com vetores e5 reais (ARCHITECTURE.md, 4.5).
    rooms_match_similarity: float = 0.7
    # Mudar de modelo só substitui as salas de uma ala quando o modelo novo já tem vetor de artigo
    # em pelo menos esta fração dos documentos que a ala tem vetorizados; até lá ficam as antigas.
    rooms_model_switch_coverage: float = 0.9
    rooms_assign_batch: int = 5000  # documentos novos atribuídos de cada vez
    rooms_interval_seconds: float = 600.0
    # Grafo entre fontes (secção 7): o `linker` periódico e os limiares das ligações `vector`.
    # O e5 comprime as semelhanças (ver a calibração na secção 7.2): 0,88 propõe, 0,94 aceita.
    link_tau: float = 0.88  # a partir daqui a ligação `vector` fica como proposta
    link_tau_accept: float = 0.94  # a partir daqui fica logo aceite
    link_batch: int = 500  # documentos vistos por execução
    link_max_per_doc: int = 10  # ligações `vector` novas por documento e execução
    link_interval_seconds: float = 60.0
    link_title_max_per_source: int = (
        3  # um título em mais documentos por fonte é genérico: ignora-se
    )
    link_max_per_key: int = 10  # `title_match` novos por título (chave) e execução
    link_max_attempts: int = 3  # um documento que falha tantas vezes seguidas deixa de ser tentado
    link_lease_minutes: float = 10.0  # passado isto um documento reclamado volta a ser tentado
    # Importação para o Wiki.js (secção 4.4) e curadoria (STRUCTURE.md, secção 5).
    import_heat_threshold: float = 3.0  # calor a partir do qual um documento vai para o Wiki.js
    import_batch: int = 100  # documentos enfileirados por execução do importador
    import_neighbors_max_per_doc: int = (
        20  # vizinhos de 1.ª ordem enfileirados por documento quente
    )
    import_interval_seconds: float = 120.0
    import_links_max: int = 200  # ligações no bloco «Ligações» de uma página
    # Fração dos artigos sem flags que o curador também vê (por hash), para afinar as regras.
    # Desligada por omissão: o curador é só para os casos que disparam flags e para a saúde.
    curation_sample_rate: float = 0.0
    # Fontes (subcadeias do id, separadas por vírgulas) e palavras-chave de saúde: sempre curadas.
    curation_health_sources: str = "wikimed,mdwiki,medicine,medicina,health,saude,saúde"
    curation_health_keywords: str = (
        "dosage,dosing,overdose,prescription,contraindication,contraindications,side effects,"
        "clinical trial,symptoms,diagnosis,medication,therapy,posologia,posologie,dosis,"
        "sobredosagem,prescrição,contraindicação,efeitos secundários,sintomas,diagnóstico,"
        "medicamento,terapêutica,tratamento clínico"
    )
    curation_health_min_hits: int = 3  # palavras-chave distintas para um artigo contar como saúde
    curation_base64_min_chars: int = 200  # bloco codificado a partir deste tamanho dispara flag
    curation_max_chars: int = 40_000  # o curador recebe o texto em partes deste tamanho
    curation_min_length_ratio: float = 0.5  # a reescrita tem pelo menos esta fração do original
    curation_fidelity_tolerance: float = 0.1  # fração de números/nomes que a reescrita pode perder
    # Curador (LLM por um endpoint compatível com a OpenAI). Sem `curator_url` o que precisa de
    # curadoria fica `pending_curator` e não se publica.
    curator_url: str = ""
    curator_model: str = ""
    curator_api_key: str = ""
    # O nome do destino «local» no painel Curadoria (vazio: «Mac (Metal)», o do ambiente).
    curator_label: str = ""
    curator_timeout_seconds: float = 600.0  # leitura da resposta; ligar demora no máximo 5 s
    # Destinos de computação e lotes (`ariadne.compute`, docs/WIKI-IMPORT.md §7). As
    # credenciais só chegam por variáveis de ambiente (segredos do Docker); o painel diz se
    # estão definidas, nunca o valor. O Kaggle precisa do utilizador (dono dos slugs) e de um token
    # (`KAGGLE_API_TOKEN`) ou da chave legada (`KAGGLE_KEY`).
    compute_tick_seconds: float = 30.0  # com que frequência o agendador olha para as corridas
    batch_part_chars: int = 16_000  # tamanho de cada pedido de curadoria enviado em lote
    # False: o que o curador reescreve (local ou lote) fica para revisão humana
    batch_auto_publish: bool = False
    batch_notebook_path: str = ""  # vazio: deploy/gpu/ariadne_batch.ipynb
    # O túnel do lote (docs/WIKI-IMPORT.md, secção 7, «Túnel»): o URL público do gateway
    # (vazio = desligado: só ficheiros) e, opcionalmente, o service token do Cloudflare Access que o
    # notebook envia. `gpu_gateway_database_url` é o papel Postgres limitado do gateway.
    gpu_gateway_public_url: str = ""
    gpu_gateway_database_url: str = ""
    gpu_gateway_access_id: str = ""
    gpu_gateway_access_secret: str = ""
    # As credenciais do túnel (o JSON do `cloudflared tunnel create`, no NAS): com elas o
    # `ariadne gpu-gateway` liga o cloudflared só enquanto há corridas (compute/tunnel.py).
    gpu_tunnel_credentials_file: str = ""
    kaggle_username: str = ""
    kaggle_api_token: str = ""
    kaggle_key: str = ""  # chave legada (kaggle.json)
    kaggle_cli: str = "kaggle"
    kaggle_model: str = DEFAULT_BATCH_MODEL  # cabe numa T4 de 16 GB (ver o notebook)
    kaggle_accelerator: str = "NvidiaTeslaT4"
    kaggle_weekly_gpu_hours: float = (
        30.0  # a quota gratuita; o Kaggle mostra o valor real no perfil
    )
    kaggle_session_hours: float = 9.0  # duração máxima de uma sessão com GPU
    kaggle_quota_reset_weekday: int = 5  # 0 = segunda … 5 = sábado (UTC, 00:00)
    # Router (secção 5): limiares de similaridade cosseno, calibrados para o e5-small.
    router_tau_high: float = 0.86
    router_tau_low: float = 0.80
    # Regra histórica (secção 5, regra 6): em perguntas de história, a versão na língua original
    # do conceito entra nos resultados com o selo «fonte original».
    router_historical_rule: bool = True
    # Salas na pesquisa (secção 5, regra 5): os resultados das `router_rooms_top` salas mais perto
    # da pergunta ganham `router_room_bonus` na fusão RRF (um degrau de poucas posições).
    router_rooms_top: int = 3
    router_room_bonus: float = 0.002
    # Armazenamento do nível 3 (secção 4.5): `pgvector` (tabelas próprias) ou `mempalace` (o mesmo,
    # mais o espelho para o MemPalace; precisa do extra `ariadne[mempalace]`).
    semantic_store: Literal["pgvector", "mempalace"] = "pgvector"
    # Onde o MemPalace guarda o espelho (`ariadne mempalace sync`): DSN do Postgres e pasta do
    # palácio.
    mempalace_dsn: str = ""
    mempalace_palace_dir: Path = Path("/models/mempalace")
    mempalace_interval_seconds: float = 900.0  # com SEMANTIC_STORE=mempalace, o worker espelha
    # Tradução (LibreTranslate, STRUCTURE.md secção 9). Sem `libretranslate_url` não há tradução e
    # tudo o resto funciona na mesma. O Argos tende para PT-BR: `libretranslate_pt_code` é o código
    # que o servidor usa para português (ex.: «pt-PT» num modelo próprio).
    libretranslate_url: str = ""
    libretranslate_api_key: str = ""
    libretranslate_pt_code: str = "pt"
    translate_queries: bool = True  # a pergunta PT⇄EN também se pesquisa na outra língua
    translate_timeout_seconds: float = 30.0  # um texto
    translate_query_timeout_seconds: float = 5.0  # uma pergunta (a pesquisa não espera mais)
    translate_down_seconds: float = 30.0  # depois de uma falha de rede não se insiste tanto tempo
    translate_chunk_chars: int = 3000  # cada pedido ao LibreTranslate leva no máximo isto
    translate_max_chars: int = 20_000  # `translate` recusa textos maiores
    # Pedidos HTTP ao /mcp por minuto de cada chave `mcp` (clientes externos); 0 = sem limite. As
    # chaves `full` (as do dono) não contam. Por processo, em memória.
    mcp_client_rate_per_minute: int = 120
    # Dias até se apagar o texto das perguntas das chaves `mcp`; ficam as contagens (retention.py).
    client_query_retention_days: int = 30
    translate_concurrency: int = 4  # traduções de texto em simultâneo (o resto espera a vez)
    translate_cache_ttl_days: float = 90.0  # a cache esquece traduções mais velhas (worker `decay`)
    translate_cache_max_rows: int = 100_000  # e, passado este tamanho, as mais antigas
    # Termos técnicos que não se traduzem (separados por vírgulas), além do código e dos URL.
    translate_protected_terms: str = DEFAULT_TERMS
    # Cursos (STRUCTURE.md, secção 6.2). `courses_repo` é o repositório Git do formato canónico
    # (fora do repositório do código); no compose é o volume /courses.
    courses_repo: Path = Path("/courses")
    courses_interval_seconds: float = 120.0  # o worker `courses` revê o repositório
    # Classificação à entrada (secção 4): «id=classe» separados por vírgulas, com `article`,
    # `course` ou `book`; o id pode levar um prefixo de caminho depois de «/» (a secção da fonte).
    source_class_overrides: str = ""
    # Sandbox de código (secção 12): o Jobe, sempre por URL (no compose, atrás do jobe-gateway).
    jobe_url: str = ""
    jobe_api_key: str = ""
    jobe_timeout_seconds: float = 60.0
    # Moodle (secção 6.2): `moodle_url` é o interno; o token do serviço grava-o
    # `ariadne moodle bootstrap`.
    moodle_url: str = ""
    moodle_token: str = ""
    moodle_public_url: str = "http://localhost:8805"  # como o utilizador o vê (nas respostas)
    moodle_timeout_seconds: float = 300.0  # publicar um curso inteiro numa só chamada
    # Classes de licença (STRUCTURE.md, secção 7): o conteúdo `private` só se envia (curador,
    # embeddings por HTTP, tradutor, Moodle, Wiki.js, Jobe) a serviços nestes hosts, separados por
    # vírgulas (`*.casa` serve para um sufixo). Vazio = os serviços do compose e o localhost.
    private_allowed_hosts: str = ""
    # Livros (STRUCTURE.md, secção 6.3). `books_dir` é a biblioteca do Calibre (volume partilhado
    # com o Calibre e o Kavita; no server e no worker só de leitura). O Calibre fala-se pelo agente
    # HTTP `calibre_url` (com chave); o Kavita, pela API (`kavita_url`) com a chave de API do
    # utilizador do Ariadne (`ariadne kavita bootstrap` grava-a).
    books_dir: Path = Path("/books")
    books_interval_seconds: float = 120.0  # o worker `books` revê a tabela e o Kavita
    books_max_bytes: int = 300 * 1024 * 1024  # `books add` recusa ficheiros maiores
    calibre_url: str = ""
    calibre_api_key: str = ""
    calibre_timeout_seconds: float = 900.0  # uma conversão pode demorar
    kavita_url: str = ""
    kavita_api_key: str = ""
    kavita_public_url: str = "http://localhost:8806"  # como o utilizador o vê (nas respostas)
    kavita_library_id: int = 0  # a biblioteca do Ariadne no Kavita (`kavita bootstrap` grava-a)
    kavita_books_path: str = "/books"  # onde o Kavita vê a biblioteca do Calibre
    kavita_timeout_seconds: float = 30.0
    # Catálogo do Kiwix (STRUCTURE.md, secção 4; Fase 12): o feed OPDS das entradas, onde o
    # dashboard escolhe os ZIM a descarregar para `zim_dir`. O pedido ao catálogo guarda-se em
    # cache `kiwix_catalog_ttl_seconds`. Um catálogo em localhost (testes) aceita espelhos locais.
    kiwix_catalog_url: str = "https://library.kiwix.org/catalog/v2/entries"
    kiwix_catalog_ttl_seconds: float = 300.0
    kiwix_catalog_timeout_seconds: float = 30.0
    zim_download_timeout_seconds: float = 60.0  # sem receber bytes durante isto, o pedido cai
    zim_download_free_margin_mb: int = 512  # espaço livre que tem de sobrar depois da descarga
    zim_wait_served_seconds: float = 90.0  # quanto o worker espera que o kiwix passe a servir o ZIM
    # Backups e releases (STRUCTURE.md, secção 16; Fase 12). O backup local (`ariadne backup
    # create`) grava os pg_dump em `backup_dir`; a release externa (`ariadne release …`) é só a
    # classe aberto, encriptada com age para uma chave PÚBLICA (a privada nunca está no sistema),
    # em partes de no máximo 2 GB. `release_enabled` só liga a publicação e o agendamento:
    # construir e ensaiar (--dry-run) funciona sempre.
    backup_dir: Path = Path("/backups")
    backup_keep: int = 4  # backups locais que ficam
    # Além da base do Ariadne (DATABASE_URL), as bases do Wiki.js e do Moodle que existirem.
    backup_extra_databases: str = "wikijs,moodle"
    release_enabled: bool = False
    release_age_recipient: str = ""  # age1…: a chave pública do destinatário
    release_age_bin: str = "age"
    release_part_bytes: int = 2_000_000_000  # tamanho máximo de cada parte (o teto é 2 GB)
    release_interval_days: float = 7.0  # a release semanal
    release_check_interval_seconds: float = 3600.0  # com que frequência o worker vê se é devida
    release_retention: int = 8  # releases que ficam (no disco e no GitHub)
    release_rehearsal_days: int = 90  # o ensaio de restauro está em atraso passado isto
    # Autenticidade (STRUCTURE.md, 16): o MANIFEST.json assina-se com uma chave de assinatura
    # própria (ssh-keygen -t ed25519; distinta da chave age). A pública fixa-se fora do GitHub; a
    # privada é um ficheiro só de leitura montado no worker.
    release_signing_key_file: str = ""
    release_signing_pubkey: str = ""  # «ssh-ed25519 AAAA…»
    backup_stale_hours: float = 6.0  # um backup/release «a correr» sem fim passado isto falhou
    release_github_repo: str = ""  # «dono/repositório»
    release_github_token: str = ""
    release_github_api_url: str = "https://api.github.com"
    release_github_timeout_seconds: float = 600.0  # o envio de uma parte de 2 GB demora
    # Chave do principal usado pelo transporte stdio do MCP (`ariadne mcp-stdio`).
    ariadne_api_key: str = ""
    # Cofre das integrações (`ariadne.vault`): os segredos e as opções das integrações ficam na
    # base de dados, cifrados com esta chave (a única que fica no Docker). Gera-se com
    # `ariadne secrets generate-key`. Os valores do cofre ganham aos das variáveis de ambiente.
    ariadne_master_key: str = ""
    vault_cache_seconds: float = 5.0  # com que frequência o servidor e o worker releem o cofre

    @model_validator(mode="after")
    def _only_allowed_models(self) -> "Settings":
        """Nenhum modelo chinês (`ariadne.models_policy`): recusa-se no arranque."""
        assert_allowed_model(self.kaggle_model, "KAGGLE_MODEL")
        assert_allowed_model(self.curator_model, "CURATOR_MODEL")
        assert_allowed_model(self.embedder_model, "EMBEDDER_MODEL")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # database_url vem do ambiente
