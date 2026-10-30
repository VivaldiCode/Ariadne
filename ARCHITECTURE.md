# Ariadne: Architecture

> **Ariadne** is the thread that guides you through the labyrinth. A model does not need to know the whole library; it needs to know how to follow the thread to the answer.

> **About this document.** This is an English summary of the maintainer's architecture document, which is written in Portuguese. It keeps the section numbering of the original (code comments cite sections such as "ARCHITECTURE.md §9"), keeps the decisions and the reasons behind them, and drops session history, completed-phase logs and private operating details. Sections that matter little to outside readers are short. For the repository layout see [`STRUCTURE.md`](STRUCTURE.md); for running the stack on one machine see [`docs/STANDALONE.md`](docs/STANDALONE.md); for the training plan see [`ARCHITECTURE-TRAINING.md`](ARCHITECTURE-TRAINING.md).

## Contents

1. Vision and goals · 2. Design principles · 3. Overview: the three levels · 4. Components · 5. Search flow (router) · 6. Usage-driven indexing (neighbor logic) · 7. Knowledge graph: links between ZIM files · 8. Links between languages · 9. Embeddings and hardware constraints · 10. Data model (Postgres) · 11. MCP interface and API · 12. Web application · 13. Deploy (Docker Compose) · 14. Decisions to fix now · 15. Final phase: an LLM trained to search · 16. Roadmap by phase · 17. Risks and open questions · 18. Getting started guide · 19. Glossary · 20. Public version (open source)

---

## 1. Vision and goals

LLMs, open-weight ones included, are trained on dated, static information. Their knowledge is frozen inside the weights.

**Ariadne** is a **large offline context memory**: a knowledge source that agents (through MCP) and people (through a web app) can query. It is built from the Kiwix library (ZIM files: Wikipedia, Stack Exchange, Gutenberg, manuals and so on).

The end goal, and the reason the project exists, is to **train an LLM that does not need to know everything but knows very well where and how to look** inside this service. The model gets into the habit of searching here, citing the source and distrusting its own memory.

### Goals

- Serve offline knowledge to agents through **a single MCP server**.
- Turn a library of isolated ZIM files into a **connected knowledge graph**, across files and across languages.
- Keep compute cost low with **usage-driven vector indexing** instead of embedding everything up front.
- Give visibility (what is used, by whom, how successfully) and catalog management through a web app.
- Stay **environment-agnostic**: one `docker compose up` on any machine.
- Produce **training data** for the searching LLM as a by-product of use.

### Non-goals (for now)

- It is not an open-web search engine. The corpus is the local library.
- It does not replace Kiwix or Wiki.js. It orchestrates them.
- No multi-tenancy or SaaS in the first version (the data model does not prevent it).

---

## 2. Design principles

1. **No isolated content.** Every article should be linked to others wherever possible: inside its ZIM, across ZIMs and across languages. This is the top priority.
2. **Pay for use, not for volume.** The expensive work (chunk embeddings) happens where there is demand.
3. **Graceful degradation.** If level 3 does not know, fall back to level 2 and level 1. The answer is never "I don't know" because an index is missing.
4. **Full traceability.** Every answer names its source (ZIM, article, version). Every graph edge records its origin and score.
5. **Modest hardware first.** Everything must work on an old server with no useful GPU. Acceleration is optional.
6. **Pluggable sources.** ZIM is the first source, not the only one.
7. **Usage is data.** Every query is logged so it can feed statistics, index warm-up and model training.

---

## 3. Overview: the three levels

| Level | What | Technology | Strengths | Limits |
|---|---|---|---|---|
| **1: Archive** | Compressed ZIM files | Kiwix (`kiwix-serve`, `python-libzim`) | Compact, complete, updatable from the Kiwix catalog | Title/full-text search only; content isolated per file |
| **2: Wiki** | ZIM content decompressed and rewritten as linked pages | Wiki.js + Postgres | Searchable, editable, lets us **create links between files** | Large on disk; heavy import |
| **3: Semantic** | Vectors for what is used most, plus its neighbors | Postgres + pgvector, organized as wings and rooms (MemPalace-style) | Semantic, multilingual, fast search | Partial coverage (grows with use) |

```
                  +----------------------------------------------+
   Agents --MCP-->               ariadne-server                   |<-- Web app (React)
   (Claude,       |  +--------+  +------------+  +------------+  |
    own LLM)      |  | Router |->| Access log |->|  Indexing  |  |
                  |  +---+----+  +------------+  |   queue    |  |
                  |      |                       +-----+------+  |
                  +------+-----------------------------+---------+
                         |                             | worker
         +---------------+---------------+-------------v---------+
         v               v               v                       |
   +------------+ +------------+   +------------------+          |
   |  Level 3   | |  Level 2   |   |     Level 1      |          |
   |  pgvector  | |  Wiki.js   |   |  kiwix-serve /   |<-- Kiwix |
   | + rooms    | | (Postgres) |   |   libzim (ZIM)   |   catalog|
   +------------+ +------------+   +------------------+          |
         ^              ^                   |                    |
         |              +---- importer <----+                    |
         +---------------- embeddings (CPU) <---------------------+
```

---

## 4. Components

### 4.1 `ariadne-server` (core, Python)

A single **Python 3.12 + FastAPI** process that exposes:

- the **MCP server** (Streamable HTTP; stdio optional for development);
- the **REST API** for the web app;
- the **search router** (section 5);
- the **access log** (asynchronous batched writes to Postgres).

Reference libraries: `fastapi`, `mcp` (the official Python SDK), `sqlalchemy 2` + `alembic`, `psycopg 3`, `pgvector`, `python-libzim`, `httpx`.

### 4.2 Workers (Python, same code, separate process)

Background jobs use a queue in Postgres itself (`SELECT ... FOR UPDATE SKIP LOCKED`), so the first version needs no Redis.

| Worker | Role |
|---|---|
| `indexer` | Handles `index_document` jobs (splits the document into chunks and creates the missing embeddings in batches, within the daily budget) and `article_vector` jobs (the cheap per-article vector) |
| `neighbors` | Periodic task: finds "hot" documents not yet expanded and enqueues `index_document` for their neighbors (section 6) |
| `importer` | Reads ZIMs and creates/updates Wiki.js pages (level 2) |
| `linker` | Periodic task: links documents from different sources by title and by article vector (section 7). Cross-language links come from the `ariadne concepts` importers (section 8) |
| `rooms` | Periodic task: clusters each wing's article vectors into rooms and assigns new documents (section 4.5) |
| `mempalace` | Periodic task, only with `SEMANTIC_STORE=mempalace`: mirrors new drawers to MemPalace (section 4.5) |
| `curator`, `compute` | LLM curation of imported pages and other model-bound jobs (section 4.4.1) |
| `courses`, `books` | Keep the course and book indexes in sync with their canonical stores (sections 4.7, 4.8) |
| `catalog`, `release` | Heavy jobs: ZIM downloads from the Kiwix catalog, and the weekly encrypted release (section 4.9). They run in a separate `worker-heavy` service so a multi-gigabyte job never stalls indexing |
| `decay` | Periodically compacts heat (stores the decayed value; reads already decay in real time) and expires old translation cache rows |

### 4.3 Level 1: Kiwix

- `kiwix-serve` for human reading and text search (`/search` and suggest endpoints).
- `python-libzim` for direct programmatic reads (articles, metadata, internal links) without going through HTTP.
### 4.4 Level 2: Wiki.js

- A Wiki.js instance with its own database on the same Postgres server.
- The `importer` converts ZIM HTML to clean Markdown and publishes it through the Wiki.js **GraphQL API**.
- Wiki.js is **not a copy**. It is a **re-reading** of the ZIM, plus "Links" blocks generated by the `linker` (links to other ZIMs and other languages).
- Page paths: `<locale>/<domain>/<subdomain>/<title-slug>`, using a curated domain tree (`ariadne/wiki/domains.yaml`). The locale is the page's language (see [`docs/WIKI-IMPORT.md`](docs/WIKI-IMPORT.md)).
- Import is **selective and incremental**. The whole of Wikipedia is never imported at once. Priority goes to articles touched by use, their neighbors, and collections chosen in the app.

Key design points:

- **Behind an interface.** Everything goes through `WikiBackend` (`ariadne/wiki/base.py`); `wikijs.py` is the Wiki.js 2.5 implementation. Wiki.js 3 is still alpha.
- **Converter.** HTML to Markdown with `markdownify`. It reflows text, strips repeated source boilerplate and hidden text, keeps tables and code in Wiki.js form, and uploads ZIM images as Wiki.js assets. Non-article content (redirects, indexes, books) is set aside first.
- **Provenance on every page.** Each page carries a header with its origin, the body, a "Links" block and the license.
- **Internal links** are stored as `ariadne:<path>` and resolved at publish time (to the Wiki.js page if it exists, otherwise to Kiwix). This keeps the page fingerprint independent of which pages exist.
- **Router integration.** Wiki.js `pages.search` runs in parallel with Kiwix when the router needs a fallback (section 5). Without Wiki.js configured, or when it is down, search degrades without error.

### 4.4.1 Content curation

Imported content is untrusted. The normal path is **deterministic and LLM-free**: convert, screen by flags and, **with no flags, publish directly**. Only items that trigger flags, and health content, go to the curator LLM.

- **Flags** (`ariadne/curation/flags.py`, regular expressions over clean text, HTML and hidden text):

  | Flag | Detects |
  |---|---|
  | `injection` | "ignore previous instructions" and paraphrases in several languages, "note to AI assistants", `<system>` and chat-template tags. Text is normalized first (NFKC, no accents, no invisible characters, collapsed whitespace) |
  | `hidden_text` | CSS and attributes that hide text (`display:none`, tiny fonts, off-screen positioning, `aria-hidden`, `sr-only`, text colored like the background). Hidden text never enters the page |
  | `encoded_blob` | long base64 sequences |
  | `invisible_chars` | bidi controls, the Unicode Tag block, runs of zero-width spaces |
  | `dangerous_command` | `curl ... \| sh`, `rm -rf /`, reverse shells, fork bombs, `mkfs`, `dd of=/dev/...` |
  | `clinical_dose` | dosage-like quantities, but only in a clinical context |
  | `health` | source name or vocabulary that marks medical content |

- **Curator** (`curator.py`): any OpenAI-compatible endpoint (`CURATOR_URL`, `CURATOR_MODEL`, `CURATOR_API_KEY`), temperature 0. The prompt treats the article as data between delimiters (with a random nonce) and tells the model to ignore instructions inside it. It returns JSON (`verdict`, `reason`, `issues`, `markdown`). Its output is **not trusted**: it is sanitized until stable, must keep at least 50% of the original length, and **may not retain** `injection`, `hidden_text`, `invisible_chars`, `dangerous_command` or `encoded_blob` (the screening runs again on the rewrite). Anything that fails becomes `needs_review`.
- **No silent publishing by default.** Even a rewrite that passes every check is published automatically only with `BATCH_AUTO_PUBLISH=true`. The default is `false` everywhere: items wait as `needs_review` for a human to publish. An empty rewrite never publishes the original, and a flagged item is published only after curation or human approval. Without `CURATOR_URL`, items stay `pending_curator` and level 1 keeps serving.
- **Fidelity check** (`fidelity.py`): deterministic. Quantities (number + unit) get zero tolerance. Numbers, months and proper nouns: nothing new, and at most `CURATION_FIDELITY_TOLERANCE` (10%) fewer, since a legitimate curator removes repetition. Failure means `needs_review`, with a report.
- **Model policy** (`ariadne/models_policy.py`): the curator, embedder and batch models are checked at startup against a maintained deny-list; a refused model fails fast.
- **`curation` table** (one row per document): `fingerprint` (sha256 of the clean text), `flags`, `decision` (`published` | `pending_curator` | `needs_review` | `rejected`), `fidelity`, the original, curated and published Markdown, and review metadata. While new text has not passed, the previous page stays.
- **Human review** (`user` principals only): `GET /api/curation`, `GET /api/curation/item`, `POST /api/curation/review`. The review request must carry the `fingerprint` the reviewer saw; if the text changed meanwhile the server answers 409. `reject` withdraws published text, leaving a notice. Nothing is ever deleted.
- **Safe output** (`ariadne/safety.py`). Through MCP, `read` returns text between `<<<ARIADNE_REFERENCE_CONTENT ...>>>` markers with a note that it is data and never instructions, plus `flags` if screening caught anything. `search` excerpts are wrapped in `<<<DATA>>>` markers. Content cannot close the delimiter. Titles (documents, sections, links, results, neighbors) are screened too: a title that looks like instructions is wrapped and the response carries `title_injection`. The REST API returns raw text because it feeds the app.

### 4.5 Level 3: pgvector + wings and rooms

- **Storage:** Postgres with `pgvector` and **HNSW** indexes (fits in memory with 128 GB).
- **Organization:** the MemPalace hierarchy (wings, then rooms) is the logical structure, implemented in Ariadne on its own tables, which remain the source of truth.
  - **Wing** = a source/ZIM (`source.wing`, for example `wikipedia_pt`). Search can filter by it (`wings`).
  - **Room** = a topic inside a wing (for example "Fado · Amália Rodrigues · Portuguese guitar"), stored in `room`. Rooms are **computed**, never written by hand.
- **How rooms are computed** (`ariadne/clustering.py`, `ariadne/rooms_build.py`):
  - Spherical k-means in numpy (no scikit-learn) over each wing's **article vectors** (section 7.1). The number of rooms is about the square root of half the article count, between `ROOMS_MIN` (8) and `ROOMS_MAX` (200). A wing with fewer than 16 vectorized articles gets none.
  - Deterministic: k-means++ seeding uses a per-wing seed, so the same data gives the same rooms. Large wings train on a hash-selected sample, then assign everything.
  - A room's **label** is its three titles nearest the centroid, joined by " · ".
  - **Room ids are stable.** On recompute, each new room inherits the id of the old room with the most similar centroid (cosine with the wing mean removed, at least `ROOMS_MATCH_SIMILARITY` = 0.7), by greedy matching.
  - New rooms are created `ready = false` and only appear when the whole wing finishes, so a query mid-recompute never sees empty labels.
  - Changing the embedding model swaps a wing's rooms only once the new model covers at least 90% of the wing's vectorized articles.
- **Why 0.7.** e5 vectors share a large common component (median cosine between unrelated articles is 0.79). Without removing the wing mean, centroids of different rooms reach 0.85 to 0.99 cosine and the originally planned 0.9 matched nearly everything. With the mean removed, different rooms stay below about 0.61 while the same room across rebuilds is 0.9 or more. The threshold sits between the two and should be re-measured on a full-size library.
- **When:** the `rooms` worker runs every 10 minutes per wing, under a per-wing Postgres advisory lock. It recomputes if the wing has no rooms or has grown by `ROOMS_GROWTH` (25%); otherwise it assigns only new documents to the nearest centroid. `ariadne rooms build|list` do the same by hand.
- **Use:** in search (section 5, rule 5) as a filter and a fusion bonus, and in neighborhoods (section 6.3).
- **MemPalace is only a mirror.** On inspection (mempalace 3.11) the package does not work as Ariadne's index: it requires `chromadb` (native binaries not validated on a CPU without AVX), and its pgvector backend keeps drawers in its own table, so using it as the index would duplicate chunks, vectors and the vector engine. So the structure is Ariadne's, and MemPalace sits behind the `SemanticStore` interface as an **optional extra**:
  - `SEMANTIC_STORE=pgvector` (default): `PgVectorStore`.
  - `SEMANTIC_STORE=mempalace` (`uv sync --extra mempalace`): `MemPalaceStore`, the `PgVectorStore` plus a **one-way mirror** Ariadne to MemPalace. Each embedded chunk becomes a drawer with `wing`, `room`, `source_file` and `title` metadata, for navigation and export, not search. `MEMPALACE_DSN` is mandatory, and the password is never written into the palace.
  - If the extra is missing, fails to import, or `MEMPALACE_DSN` is unset, the server starts with `PgVectorStore`, warns, and reports it in `GET /health` (`checks.semantic_store`).

### 4.6 Web application (React)

See section 12.

### 4.7 Courses

A content base where a **canonical format in Git** is the source of truth, Moodle (with CodeRunner) is presentation only, and Jobe runs the code. Everything sits behind interfaces: `Source` (a lesson as a document), `CourseBackend` (publish), `Sandbox` (execute).

- **Classification at intake** (`ariadne/sources/classify.py`): each source is an `article`, `course` or `book`, decided from ZIM metadata, with the boundary rule "has exercises means course; reference only means book" and manual overrides in `SOURCE_CLASS_OVERRIDES`.
- **Canonical format** (`ariadne/courses/`), in a separate Git repository (`COURSES_REPO`): `<course>/<version>/course.yaml`, then `<NNN>-<module>/module.yaml` and `<NNN>-<lesson>.yaml`. Lessons hold blocks (`text`, `code_example`, `exercise` with tests, `quiz`, `solution`, `video`), prerequisites (`requires`), origin, license and a **license class**. The pydantic schema forbids unknown fields and rejects prerequisite **cycles**. Text uses YAML literal blocks, so Git diffs stay clean and a round trip is exact.
- **Index.** Course tables are rebuilt from Git by `ariadne courses sync`. Each lesson is also a **document** (`course:<course>/<module>/<lesson>`), so the indexer, article vectors, linker, `read` and `neighbors` work unchanged. Prerequisites are **directed** `requires` edges.
- **freeCodeCamp extractor.** Reads the ZIM's curriculum index and per-challenge Markdown into lessons with exercise and solution. Its chai `assert.*` tests (written for a browser) run in Jobe through a Node template with a chai shim and the student code in an isolated `vm` context; tests that cannot be mapped stay "unmapped" with the reason.
- **Curation.** The section 4.4.1 screening applies to lesson text and code. Only a `published` lesson goes to Moodle; others are **held** (`lesson.held_reason`).
- **Moodle + CodeRunner** (`deploy/moodle`). A custom image on the official Moodle HQ PHP image (Moodle 4.5 LTS, CodeRunner, a small `local_ariadne` plugin). **Each course version is one Moodle course**; a lesson with a gradable exercise becomes a quiz. The official solution never leaves the canonical store.
- **Jobe** (`ariadne/sandbox`). A REST client with CPU, memory, process, disk and output limits. Jobe sits on an `internal: true` Docker network with no route out; only a `jobe-gateway` (nginx, API key, request limit) bridges it to the Moodle, server and worker network.

### 4.8 Books

**Calibre** holds the library (source of truth for files and metadata), **Kavita** is the web reader, and Ariadne extracts text by chapter. Behind interfaces: `LibraryBackend` and `Source` (a chapter as a document). **License classes** exist system-wide from here on.

- **Calibre** runs in a thin container around the official binary behind a small HTTP agent with a mandatory API key, keeping hundreds of megabytes of Qt out of the server image.
- **Intake** (`books/add.py`): DRM check first, then a mandatory license class and license, then Calibre stores the file, then extraction confirms it is readable. A failure after Calibre has the book removes it again.
- **Extraction** (`books/extract.py`) is pure Python and **always runs in a limited subprocess**, so a hostile file kills the child, not the server. There are limits on size, file count, compression ratio and encoding.
- **Search.** A dedicated table with a `tsvector` (GIN index) rather than Kavita's index, because Kavita only searches metadata and because the searched text is exactly what `read` returns, which gives exact citation positions. Chapter results carry `book`, `position`, `citation` and `reader_url`; `read` accepts `offset` to continue from a citation.
- **License classes** (`ariadne/licenses.py`). The **most restrictive class always wins**: a `private` override persists and source sync never lowers it. `assert_exportable` guards everything that leaves the system, and `assert_may_send` guards everything that **sends** text to a service (curator, HTTP embeddings, translator, Moodle, Wiki.js, Jobe). Private content only goes to hosts on `PRIVATE_ALLOWED_HOSTS` (default: localhost and the compose services); blocks are recorded in `license_block`.
### 4.9 Kiwix catalog, backups and releases

- **Catalog** (`ariadne/zimcatalog/`). The Kiwix catalog is an Atom/OPDS feed. The client refuses XML with DTDs/entities and oversized responses, and caches briefly. Each entry has a `.meta4` (Metalink) file with checksums.
- **Download.** `POST /api/catalog/{book}/download` (administrators) re-fetches the entry from the catalog (the URL **never** comes from the request) and enqueues one `catalog_download` job per book. The worker refuses a download without a sha-256 (mirrors are third parties) unless `allow_unverified` is set, validates the file name (never a path), checks free space, downloads to `.part` with resumable `Range` requests, verifies, renames, registers the source and waits for Kiwix to serve it.
- **Removing a source** **archives** it: the ZIM moves to `ZIM_DIR/.archive/`, documents and index stay inactive, and Kiwix stops serving it. Deleting the file requires `?delete_file=true&confirm=<source id>`.
- **Local backup** (`ariadne/backup.py`): `pg_dump --format=custom` of each database plus the courses repository, written to a temp folder and renamed at the end, with a `MANIFEST.json` of sha-256 sums. Passwords go through `PGPASSWORD`, never the command line. `verify_backup` re-checks the sums and that `pg_restore --list` reads every dump.
- **Release** (`ariadne/release/`): a weekly, encrypted, signed, split export of what the license policy allows.
  - **Content:** open sources only, published pages, open course versions and a whitelist of settings.
  - **Packaging:** streams `tar.gz` into `age -r <public key>` and splits it into parts of at most 2 GB with a sha-256 each. **No plaintext tree is ever written to disk.**
  - **Authenticity:** an ed25519 `ssh-keygen -Y sign` signature over the manifest, checked **before** the manifest is read (in `release verify`, `publish` and the generated `restore.sh`, which takes the public key from its caller, never from the archive).
  - **Rehearsal:** with the private key, decrypt to a temp folder, load the data into a fresh database under an unprivileged role, compare counts and confirm that nothing private is present.
  - **Publishing** uses the GitHub REST API: draft, upload, publish, retention. Interrupted drafts resume.

---

## 5. Search flow (router)

```
question --> embedding (CPU, one sentence, ~ms)
                |
                v
        pgvector top-k (level 3)
                |
      sim >= tau_high? --yes--> answer (level 3) --+
                |no                                |
                v                                  |
   parallel text search:                           |
   Wiki.js (level 2)  +  kiwix-serve (level 1)     |
                |                                  |
     merge and rerank (RRF + vector rescoring)     |
                |                                  |
     answer + enqueue the documents used --------->+
                                                   v
                                   log access (who, what, level, score)
```

Rules:

1. **Thresholds** (`ROUTER_TAU_HIGH=0.86`, `ROUTER_TAU_LOW=0.80`, calibrated below). `sim` is the cosine similarity of the best chunk. `sim >= tau_high` answers from level 3 alone. `tau_low <= sim < tau_high` is the gray zone. `sim < tau_low` uses only the fallback.
2. **Gray zone:** returns level 3 **and** fallback results, merged. If the fallback fails, it answers from level 3 and warns.
3. **Fusion** by *Reciprocal Rank Fusion* (k = 60) between the level 3 list (one result per document, its best chunk) and the fallback list. Fallback candidates are first rescored with an embedding of "title. excerpt" against the question embedding. A document that appears in several lists keeps the highest level.
4. Every document returned from the fallback is enqueued for indexing (`index_document`, one entry per document) with priority equal to its `heat`. Documents already indexed with the current model and source version are not re-enqueued.
5. **Optional filters:** language, source, **wing** (`wings`), **room** (`rooms`), date range. `rooms` restricts to the wings' sources and to the **documents in those rooms**. Unknown rooms or rooms outside the requested wings are an error whose message says where to see ids.
6. **Historical rule** (section 8.6; `ROUTER_HISTORICAL_RULE`, on by default): if the question is historical and one of the top three results belongs to a concept whose original language is known and differs from the question's, the concept's version in that language is inserted right after, with `original_source: true`.
7. Every answer carries `source_id`, title, Kiwix URL and Wiki.js URL (if any), excerpt, originating level and score.
8. If the embedder fails (model not downloaded, endpoint down), search degrades to level 1 and the answer carries a warning. Level 3 never takes search down (principle 3).
9. **Translated query** (section 8.7, only with `LIBRETRANSLATE_URL`): when a fallback is needed, a PT or EN question is also translated to the other language and searched in Kiwix and Wiki.js. These lists join the same RRF fusion, and a result found **only** that way is marked `translated_query: true`. The original search is never skipped.

### 5.1 Threshold calibration

e5-small compresses similarities: unrelated texts land around 0.75 to 0.85 and good question/passage pairs around 0.82 to 0.90, so an absolute threshold separates them poorly. Measured with ONNX (fp32 and int8 agree within 0.01) on 416 chunks from 60 articles of the test ZIM, with 12 on-topic and 8 unrelated questions:

Best-chunk cosine was 0.81 to 0.90 for on-topic questions (EN and PT, short and long; median about 0.84) and 0.79 to 0.87 for unrelated ones (median about 0.81).

So `tau_high = 0.86` skips the fallback only when the chunk is really good, and `tau_low = 0.80` sends noise-level matches to the fallback. A false "high" is expensive (it answers with unrelated content); a false "gray" is cheap (the fallback also joins the fusion), so `tau_high` is conservative. The sample is small and single-topic. Recalibrate once `query_log` has feedback. 

---

## 6. Usage-driven indexing (neighbor logic)

The central idea: **the vector index is a semantic cache that warms with use.** Everything queried is embedded; what is queried a lot makes its neighborhood get embedded.

### 6.1 Decaying counts

Each document and chunk has a "heat" score: `heat(t) = heat(t_prev) * 2^(-(t - t_prev) / half_life) + event_weight`. The initial `half_life` is 30 days (configurable). `event_weight` is 1.0 for a result used in an answer, 0.3 for a result only shown and 2.0 for explicit positive feedback. Reads use the formula in real time, so the `decay` worker only compacts.

### 6.2 Trigger

When a document's current heat passes the threshold `NEIGHBORS_HEAT_THRESHOLD` (initially 5), the `neighbors` worker enqueues its neighbors. Every `NEIGHBORS_INTERVAL_SECONDS` (60) it expands the `NEIGHBORS_BATCH` (20) hottest documents not yet expanded, or whose last expansion is older than one half-life and are still hot. A document is claimed with one `UPDATE ... SET expanded_at = now()` on a row chosen with `SKIP LOCKED`, so two workers never expand the same document.

### 6.3 Neighbor kinds (cheapest first)

| Kind | Origin | Cost | Notes |
|---|---|---|---|
| Adjacency | Paragraphs/sections of the same article | Free | Completes the hot article |
| Internal links | Links inside the ZIM (article to article) | Free | The ZIM's native graph |
| Cross links | Edges created by the `linker` (other ZIMs, other languages) | Free (precomputed) | Sections 7 and 8 |
| Room | Other documents in the same room | Cheap | Limited to the room's hottest |
| Semantic | k nearest neighbors in article-vector space | Cheap | Uses article vectors (section 7) |

Weights (`NEIGHBORS_WEIGHT_*`): adjacency 1.0, cross links 0.8, internal links 0.6, semantic 0.5, room 0.4. Internal links are stored in `link` as `zim_internal` edges.

### 6.4 Limits

- **One hop** from the hot point.
- Enqueued neighbors **do not gain heat**: only real use makes them expand too.
- At most `NEIGHBORS_MAX_PER_DOC` (50) new neighbors per expansion, highest priority first.
- **Daily budget** of chunks to embed (initially 20,000/day, tunable to CPU capacity). It is the global brake: a job that no longer fits is deferred (without spending retries) until the next UTC midnight. Nothing is lost. `GET /api/jobs` shows what is left.
- **Queue priority** = source `heat` times neighbor-kind weight, with a `dedupe_key` so `index_document` is never duplicated, only raised in priority.
- **Cold start:** levels 1 and 2 guarantee an answer. Optionally seed level 3 offline with chosen collections: `ariadne index enqueue --source <id> [--paths file] [--limit N] [--priority P]` (default priority 0.1, below any real use).

---

## 7. Knowledge graph: links between ZIM files

A ZIM already contains a graph **inside** each file. What is missing, and what is the heart of the project, is linking **different files** (for example a history article on Wikipedia, a book on Gutenberg and an answer on the History Stack Exchange).

### 7.1 Article vectors (cheap)

- **One vector per article**: the title plus the first substantive paragraph of the introduction (at least 40 characters, truncated to 800), embedded as `passage:`. Article entries only, no redirects or resources.
- They number a few million in total, created on a fast workstation in the initial phase or slowly on the server. They live in `document_vector` (HNSW, cosine).
- They **do not replace** level 3 chunk vectors. They are for discovering links and for semantic neighborhoods.
- **Initial bulk load:** `ariadne vectors build --source <id> [--limit N] --out file.parquet` writes Parquet (`source_id, path, title, language, version, model_id, embedding`, plus `aliases`, the redirect titles pointing to the article). `ariadne vectors import file.parquet` upserts in batches, is idempotent, and refuses a file with a different `model_id` or dimension.
- **On the server:** when the `indexer` finishes a document that has no article vector, it enqueues an `article_vector` job. This does not count against the daily budget (which counts chunks).

### 7.2 Linking strategies

1. **Equal titles and entities** across ZIMs (`title_match`): the normalized title (casefold, collapsed whitespace, no trailing punctuation; accents count) equals that of a document in **another active source**. Redirect titles (aliases) count too. It is `accepted` (score 1.0) only for equal **main titles** in the same language. Alias matches, or matches across languages, stay `proposed` unless the article-vector cosine confirms them (at least `LINK_TAU`).
2. **Wikidata** (section 8) when a QID is available.
3. **Vector similarity** (`vector`) between article vectors of different sources: cosine at or above `LINK_TAU` (0.88) becomes a **proposal**; at or above `LINK_TAU_ACCEPT` (0.94) it is accepted immediately. At most `LINK_MAX_PER_DOC` (10) new links per document per run, most similar first.
4. **Manual**, through the web app or the API (curation, section 11.2).

**Threshold calibration** (e5-small, fp32; int8 reads 0.00 to 0.01 lower). On "title + first paragraph" vectors:

| Pair | Cosine |
|---|---|
| Same subject, same language, other source | 0.92 to 0.93 |
| Same subject, PT vs ES / PT vs EN | 0.93 / 0.89 |
| Unrelated | 0.80 to 0.85 |

Hence 0.88 to propose (catches the same subject, and lets some related ones through, which is why they stay `proposed`) and 0.94 to accept unattended (near-duplicates). Recalibrate on real ZIMs before trusting automatic `accepted` at scale.

**The `linker`** is a periodic task of `ariadne worker` (`LINK_INTERVAL_SECONDS`, 60). Each run it sees up to `LINK_BATCH` (500) documents with `linked_at IS NULL` (new ones, or ones whose title, aliases or article vector changed). A document is marked seen only when its batch finishes; meanwhile it holds a lease (`link_lease`, expiring in `LINK_LEASE_MINUTES`) and a counted attempt.

### 7.3 Every link is auditable

Each edge stores source, target, **kind** (`zim_internal`, `title_match`, `wikidata`, `vector`, `langlink`, `manual`, `requires`), **score**, the model version that produced it, status (`proposed`, `accepted`, `rejected`), who created it (`created_by`) and who reviewed it (`reviewed_by`, `reviewed_at`). `vector` edges below `LINK_TAU_ACCEPT` stay proposals until someone validates them.

**Direction.** `zim_internal` (and `requires`) are directed. Every other kind is **undirected**: one row per pair with `src_document < dst_document` (code point order), and queries look at both sides. A `CHECK` constraint enforces the order, so a pair discovered from both sides is never duplicated.

### 7.4 Materialization in Wiki.js

The `importer` adds a "Links" block to each page, grouped by kind and source. Level 2 thus becomes a **linked re-reading** of the library that people can browse, and the graph becomes visible.

---

## 8. Links between languages

For historical events, the source in the **original language** is often richer and more reliable. This is implemented.

### 8.1 Model

- **Multilingual embeddings** from the start (`multilingual-e5-small`): "Revolução dos Cravos" and "Carnation Revolution" land close together, so level 3 already crosses languages without translation.
- **Concept** (`concept`): groups the versions of one topic across languages and projects. It has a `wikidata_qid` when it comes from Wikidata, none when it comes from a `langlinks` dump. `document.concept_id` points to it. `origin_lang` is the original language and `origin_source` says how it was learned (`manual`, `country:PT`).
- **Edges**: `langlink` (same project, different languages) and `wikidata` (different projects, same QID), `accepted`, score 1, undirected. Existing edges are left alone: one rejected in curation never comes back.
- **Project and site of each source** (`source.meta`) come from the ZIM name (`wikipedia_pt_all` gives project `wikipedia`, language `pt`, site `ptwiki`). Without these keys a source is not part of the imports (for example Stack Exchange).

### 8.2 Importing Wikidata sitelinks

- **Wikidata sitelinks:** `ariadne concepts import-sitelinks <wb_items_per_site.sql.gz> [--sites ptwiki,enwiki] [--min-members 2]`. The dump is read as a stream (a robust parser for MySQL `INSERT ... VALUES`). Only sites of present sources count, and only articles that **exist in the ZIMs** (titles are looked up in the ZIM title index without reading content). Memory is bounded: a first pass only counts sites per item, then titles are resolved in slices of at most `MAX_ITEMS` (300,000) items.
- **Conflicts:** a document that already belongs to *another* QID stays there; one that appears in two groups of the same batch is ambiguous and dropped from both. Each batch takes a `pg_advisory_xact_lock`.

### 8.3 Alternative without the Wikidata dump: `langlinks`

`ariadne concepts import-langlinks <page.sql.gz> <langlinks.sql.gz> --site ptwiki`. A link that would join two *different* articles of the same language is ambiguous and ignored. Each component becomes a **QID-less** concept, which a later `import-sitelinks` merges into QID concepts.

### 8.4 Original language

- By hand (`ariadne concepts set-origin Q1187566 pt`, or `POST /api/concepts/{qid}`), or from a TSV of `QID<TAB>country` extracted from Wikidata, where a small table converts country to language. Multilingual countries and ambiguous QIDs get no origin, and hand-set values are never overwritten.
- Without data, `origin_lang` stays **NULL**. Nothing is invented.

### 8.5 `translations`

The MCP tool `translations(document_id)` and `GET /api/translations?document_id=` return the versions of the same concept in active sources and **another language**, with `original_language: true` on the original (listed first). Versions whose edge was rejected in curation do not appear.

### 8.6 Historical rule in the router

`ariadne/router_rules.py` decides a question is historical if it has one **strong** signal (a history word stem in PT/EN/ES/FR such as revolution, war, battle, treaty, dynasty; or a year after a context word, "44 BC", "476 AD") or **two weak signals**. False positives such as "GTX 1080" or "Star Wars cast" are covered by tests.

### 8.7 Translation (LibreTranslate)

- `Translator` interface (`ariadne/translate/`) with `LibreTranslate` and `FakeTranslator` implementations. `LIBRETRANSLATE_URL` points to the service; the Compose `translate` profile starts a local one. Without a URL, translation is off and everything else works.
- **Protection** (`translate/protect.py`). Before translating, code blocks, inline code, `[doc:...]` citations, URLs, identifiers and `TRANSLATE_PROTECTED_TERMS` become markers (`QXZ0000`, `QXZ0001`...) that are restored afterwards. The marker shape was measured against the real engine (a marker with a suffix made it inject words; this one survived 16 of 16 sentences). A lost or repeated marker refuses the translation, so broken code is never returned.
- **Privacy.** `LIBRETRANSLATE_URL` must point to a **local** service: question and article text goes there. Do not point it at a public service.
- **PT-PT vs PT-BR.** Argos leans Brazilian. For European Portuguese, load a dedicated package and set `LIBRETRANSLATE_PT_CODE`.

### 8.8 Known limits

- Question language detection is a function-word heuristic, so short ambiguous questions between pt/es/fr get no language and skip translation and the historical rule.
---

## 9. Embeddings and hardware constraints

This section is a hard constraint on the whole codebase.

### 9.1 Target environment (production data server)

| Resource | Value | Implication |
|---|---|---|
| CPU | 2x Intel Xeon X5550 @ 2.67 GHz (Nehalem, 2009), 16 threads | **No AVX/AVX2** (SSE4.2 only, i.e. x86-64-v2); slow inference |
| RAM | 128 GB | Plenty of room for Postgres, in-memory HNSW and caches |
| GPU | AMD RX 540 | Little VRAM; **no ROCm support**; Vulkan possible but marginal |

Everything must therefore run on **CPU only**, with no instruction beyond SSE4.2. That excludes PyTorch from the server and any native dependency that assumes AVX, FMA, F16C or BMI2.

### 9.2 Decisions

- **Production model:** small multilingual, 384 dimensions, for example `intfloat/multilingual-e5-small` (alternatives: `paraphrase-multilingual-MiniLM-L12-v2`; `embeddinggemma-300m` is to be evaluated).
- **Runs on the server on CPU**, for incremental indexing and query embeddings (one sentence at a time, acceptable latency).
- **`Embedder` abstraction** with implementations: `onnxruntime` (CPU), `llama.cpp` GGUF (CPU/Vulkan), and an OpenAI-compatible HTTP server (to delegate to another machine).

### 9.3 Risk validated early

Many prebuilt wheels (PyTorch, some `onnxruntime` builds, default `llama.cpp`) **assume AVX** and die with *illegal instruction* on a Nehalem CPU. Rules that follow:

1. `onnxruntime` CPU is the default embedder and has passed the canary on the target.
2. `llama.cpp` is built **never with `GGML_NATIVE`**. With `GGML_NATIVE=OFF`, ggml still enables AVX, AVX2, FMA, F16C and BMI2 by default, so all of them must be disabled explicitly:

   ```
   -DGGML_NATIVE=OFF -DGGML_AVX=OFF -DGGML_AVX2=OFF -DGGML_FMA=OFF -DGGML_F16C=OFF \
   -DGGML_BMI2=OFF -DGGML_AVX512=OFF -DGGML_AVX_VNNI=OFF -DGGML_SSE42=ON
   ```
3. Plan B: delegate query embeddings to another machine over HTTP.
4. A new native dependency must pass the benchmark canary (`bench/embeddings`) before it enters the `server` image.

### 9.4 Versioning

Each vector stores the model and the exact revision that produced it in `model_id`, with the convention `<repo>@<revision>` (for example `intfloat/multilingual-e5-small@614241f`). The dimension is fixed by the table (`vector(384)`; another dimension gets its own table). Changing model means a new generation of vectors side by side and a gradual migration, without deleting the old one until the new index is ready.

---

## 10. Data model (Postgres)

Database `ariadne` (Wiki.js uses a separate database on the same server). This is the shape, not the full DDL; the migrations in `server/migrations/` are authoritative.

```sql
CREATE EXTENSION IF NOT EXISTS vector;

source(id text PK 'zim:wikipedia_pt_all_nopic', kind, title, language, version, wing, status,
       license_class 'open'|'private', meta jsonb)
concept(id, wikidata_qid UNIQUE, origin_lang, label, origin_source)
document(id text PK '<source_id>/<path>', source_id, path, version, title, language, concept_id,
         room -> room(id) ON DELETE SET NULL, wiki_page_id, heat, heat_at, expanded_at,
         title_keys text[], linked_at, content_type 'article'|'lesson'|'chapter', meta jsonb)
room(id, wing, label, centroid vector(384), size, base_size, ready, updated_at)

-- Level 3
document_vector(document_id, model_id, embedding vector(384), PK(document_id, model_id))
chunk(id, document_id, ord, text, heat, heat_at, UNIQUE(document_id, ord))
chunk_vector(chunk_id, model_id, embedding vector(384), created_at, PK(chunk_id, model_id))
-- HNSW (vector_cosine_ops) on both embedding columns

-- Auditable graph edges
link(id, src_document, dst_document,
     kind   zim_internal | title_match | wikidata | vector | langlink | manual | requires,
     score, model_id,
     status proposed | accepted | rejected,
     created_by, created_at, reviewed_by, reviewed_at,
     UNIQUE(src_document, dst_document, kind))
-- CHECK: directed kinds (zim_internal, requires) aside, src_document COLLATE "C" < dst_document COLLATE "C"

-- Identity
principal(id, kind 'user'|'agent', name, role 'admin'|'member', api_key_hash UNIQUE (NULL = revoked), ...)

-- Usage log (statistics + warm-up + training)
query_log(id, principal_id, query, query_lang, filters, level_served 1|2|3, top_score, latency_ms,
          session_id, created_at)
query_result(query_id, rank, document_id, chunk_id, level, score, used, feedback -1|0|1,
             original_source, translated_query, PK(query_id, rank))
tool_call(...)  -- every MCP tool call and REST endpoint: who, which, args, duration, error
job(...)        -- the Postgres queue: type, payload, priority, dedupe_key, attempts, lock
```

Other tables, summarized: `curation` (one row per document: fingerprint, flags, decision, fidelity, original/curated/published Markdown), `translation_cache`, `collection` (hand-picked sets of documents to pre-warm), `course`/`course_version`/`lesson`/`exercise` (section 4.7), `book`/`chapter` (section 4.8; `chapter.search_tsv` is a generated `tsvector` with a GIN index), `license_block` (what outputs refused or filtered) and `backup_run` (section 4.9).

Notes:

- `tool_call` logs **every call** and links to `query_log` when the call was a search. It does not store returned text. **Full sessions for training** (question, calls, results, used, answer; section 14, point 7) are a separate layer built on `tool_call` + `query_log` + `query_result`.
- A `vector(384)` column fixes the current model's dimension. A model with another dimension uses its own table or column.

---

## 11. MCP interface and API

### 11.1 MCP tools

Principle: **few tools, well described**, so that an LLM (including the project's own, in the final phase) learns them easily.

| Tool | Input | Output |
|---|---|---|
| `search` | `query`, `lang?`, `sources?`, `wings?`, `rooms?`, `k?` | Results with excerpt, source, wing, room (`id` + label), level, score, `document_id`, `content_type`; chapters add `book`, `position`, `citation`, `reader_url` |
| `read` | `document_id`, `section?`, `max_chars?`, `offset?` | Document text (or a section, cut at `max_chars`, default 20,000), the list of sections and links as `document_id`s. Lessons add `lesson`; chapters add `book` and `citation` |
| `neighbors` | `document_id`, `kinds?`, `k?` (20) | Linked documents: `document_id`, title, source, language, `kind`, `score`, `status` (`accepted`, `proposed` or `computed`); `requires` edges carry `direction` |
| `translations` | `document_id` | Versions of the same concept in other languages, with `original_language: true` on the original |
| `translate` | `text`, `target`, `source?` | Translated text (code, citations and technical terms intact); `translated: false` + `warning` if the service fails |
| `feedback` | `query_id`, `document_id`, `useful: bool` | Confirmation (feeds `heat` and the training dataset) |
| `sources` | none | The available catalog (sources, languages, versions, wing and the largest rooms) |
| `rooms` | `wing?`, `limit?` | Rooms (topics) of one wing or all, largest first |

Authentication is by API key (`Authorization: Bearer ...`), which identifies the `principal`. Without a valid key the `/mcp` endpoint (Streamable HTTP) answers 401. Optional MCP resources: `ariadne://document/<id>`.

Keys have a scope. A `full` key (the default) sees everything and can use the whole REST API. An `mcp` key (`ariadne principal create --scope mcp`, always an `agent` with role `member`) is meant for external clients of a shared instance: it is refused by every REST route (403), sees only *open* content over MCP (private sources are not searched and do not appear in errors; a private document answers like one that does not exist), is rate-limited (`MCP_CLIENT_RATE_PER_MINUTE`), and the text of its questions is deleted after `CLIENT_QUERY_RETENTION_DAYS` (30). Its feedback is recorded but does not warm documents; its searches and reads still add the usual low heat (shown 0.3, read 1.0). Every REST write (compute and external batches, curation, links, concept origins, domains, collections, job retries) is admin-only: a `member` user can only read.

An admin manages client keys with three routes: `POST /api/principals` with `{"name": …, "kind": "agent", "scope": "mcp"}` creates one and returns the key once; `DELETE /api/principals/{id}` revokes it immediately; `POST /api/principals/{id}/erase` deletes the client for good (GDPR "delete account"): the principal, its key hash, `tool_call`, `query_log`, `query_result` (feedback) and its name in the `principal_create` log. It answers `{principal_id, erased, tool_calls, queries, query_results}`, is idempotent (`erased: false` once the principal is gone) and refuses `full` principals (409), so the owner's keys are never erased. Use an opaque id as the client's name, not an email address.

Four more admin-only routes serve the hosted offering (migration 0018). Each one refuses `mcp`, agent and `member` keys with 403, and the tests prove it route by route.
- **Suspend and resume.** `POST /api/principals/{id}/suspend` and `/resume` (idempotent) keep the key hash, so a suspended key answers 403 on `/mcp` (and is refused over stdio) and the very same key works again once resumed: the client reconfigures nothing. Only `mcp` keys can be suspended (409 otherwise, and 409 for a revoked key).
- **Idempotent creation.** `POST /api/principals` takes `external_id` in the body or an `Idempotency-Key` header (the same identifier, so both must match if both are sent). Repeating the request returns 200 with the existing principal, `api_key: null` and `idempotent_replay: true`: the secret is never shown twice. Different parameters under the same identifier answer 409, and simultaneous requests create exactly one. `GET /api/principals?external_id=` looks one up; erasing a client also frees its identifier.
- **Monthly quota.** `monthly_query_quota` (an integer of at least 1, or `null` for none; `mcp` keys only) is set on `POST` or `PATCH /api/principals/{id}`. It counts the key's tool calls in the current UTC month that did not fail; once spent, `/mcp` answers 429 with `Retry-After` until the start of next month and `Monthly request quota exceeded (N requests per month)`. It is independent of, and adds to, the per-minute rate limit.
- **Usage.** `GET /api/principals/usage?ids=1,2,3&days=30` (up to 200 ids; without `ids`, every `mcp` key) and `GET /api/principals/{id}/usage` return, per principal: calls, errors, searches and searches with no results over the interval, a daily series (one point per UTC day, zeros included), totals per tool, the last use ever, and the current month against the quota (`used`, `quota`, `remaining`, `resets_at`).

Behavior worth knowing:

- `neighbors` returns edges in both directions (rejected ones and ones from vanished sources are hidden) plus **computed semantic neighbors** (`kind: semantic`, `status: computed`, by kNN on article vectors). Order: accepted cross-source links, then proposals, semantic neighbors, and, with what remains of `k`, the source's own `zim_internal` links.
- `search` returns a `query_id` which `feedback` uses to mark `query_result.used` and `feedback`. `read` also marks as used the result of a recent search by the same principal.
- All text returned to agents is wrapped as data (section 4.4.1).

### 11.2 REST API (for the app)

OpenAPI is generated by FastAPI, and a TypeScript client is generated for the frontend (`npm run gen:api`). Main groups:

- **Search and reading:** `GET /api/search`, `GET /api/documents/{id}`, `GET /api/neighbors`, `GET /api/translations`, `POST /api/translate`, `GET /api/rooms`, `GET /api/concepts/{qid}`.
- **Graph curation:** `GET /api/links?status=proposed&kind=&document_id=&cursor=` (keyset pagination, highest score first), `POST /api/links` (manual link), `POST /api/links/{id}` (accept, reject, reopen). Writing requires a `user` principal (403 for `agent`); reading is open to all.
- **Content curation:** `GET /api/curation`, `GET /api/curation/item`, `POST /api/curation/review`.
- **Sources, catalog, courses, books, licenses:** `GET /api/sources`, `DELETE /api/sources/{id}` (archives), `GET /api/catalog`, `POST /api/catalog/{book}/download`, `GET /api/courses`, `GET /api/books`, `PUT /api/books/{id}/license`, `GET /api/licenses`.
- **Operation:** `GET /api/jobs` (queue counts, daily `budget`, failed jobs), `POST /api/jobs/{id}/retry`, `GET /api/config` (read-only: changing a threshold means editing the environment and restarting), `GET /api/backups`, `POST /api/backups/release`, `/api/principals` (administrators only; a key is shown once; the last active administrator can never be revoked; `PATCH` changes the role and the monthly quota), `GET /api/whoami`, `GET /health`.
---

## 12. Web application

Stack: **React + Vite + TypeScript**, served as static files by `ariadne-server` itself. The `server/Dockerfile` has a Node build stage and the final image carries only `web/dist`. FastAPI mounts it at `/` last, with `index.html` as the answer for client-router paths; `/api`, `/mcp`, `/health` and `/docs` never fall through to it. There is no SSR, and Node never runs at runtime.

- **API client** generated from OpenAPI (`openapi-typescript` + `openapi-fetch`; the schema is committed). TanStack Query for data, react-router for routes, plain CSS over the design-system tokens, with a single dark theme, self-hosted fonts and no third-party requests. Hand-drawn SVG charts; the graph uses Cytoscape.js (loaded only on the graph page). Routes are English (`/read`, `/graph`, `/stats`, `/sources`, `/courses`, `/books`, `/curation`, `/links`, `/licenses`, `/operations`, `/integrations`).
### Modules

1. **Reading:** unified search (language, sources, room) with level, score and "original source" / "translated question" badges; an article reader with a side panel for links (by source), other languages, sections and text translation. Lessons and book chapters have their own reader panels.
2. **Graph:** an article's neighborhood in Cytoscape, filterable by link kind (proposals dashed, computed neighbors dotted).
3. **Statistics:** queries per day and level, level 3 rate, latency, hottest and most-used documents, index coverage per source.
4. **Sources and catalog:** registered sources, disk use, indexing, import and curation status, each wing's largest rooms, and the Kiwix catalog (search, download with progress, archive, restore, delete).
5. **Curation:** the human review queue, with flags, curator decision, fidelity, original and curated text side by side, and approve or reject.
6. **Links:** accept, reject or reopen proposals, create manual links, pre-warm collections, set a topic's original language.
7. **Operations:** service health, queues and failed jobs, the daily budget, backups and releases, the effective configuration (read-only), API keys, users and roles.
8. **Courses**, 9. **Books**, 10. **Licenses:** versions, held lessons and Moodle status; the library, chapters and reader links; license classes, saved outputs and applied blocks.
11. **Integrations:** the encrypted vault of integration settings and the curation/batch destinations.

---

## 13. Deploy (Docker Compose)

The main format is **one `docker-compose.yml`** that brings the full stack up with `docker compose up -d`. The one at the repository root is the **standalone distribution**: everything on one machine, CPU only and no AVX, public or repository-built images, profiles for optional services and an automatic bootstrap (`ariadne bootstrap`). See [`docs/STANDALONE.md`](docs/STANDALONE.md). The operator's own deployment notes are not part of this repository.

Core services (always started): `postgres` (pgvector), `kiwix`, `server` (API + MCP + web app on one port), `worker`, `worker-llm`, `worker-heavy`.

Compose profiles:

| Profile | Adds |
|---|---|
| `wiki` | Wiki.js (level 2) |
| `books` | Calibre + Kavita |
| `courses` | Moodle + CodeRunner + Jobe (+ `jobe-gateway`) |
| `translate` | LibreTranslate with Argos models |
| `llm` | A local curator LLM (`llama-server`) |
| `tunel` | The external-GPU batch gateway (outside `all`) |
| `all` | Everything above except `tunel` |

Design notes:

- **Three worker services.** `worker` runs the everyday jobs. `worker-llm` (`--only curator,compute`) isolates curation, which on a CPU curator takes minutes per page and would otherwise stall import and indexing on a worker that runs one job at a time. `worker-heavy` (`--only catalog,release`) runs ZIM downloads and the weekly release.
- **Service URLs default from the compose file** (for example `${WIKIJS_URL-http://wikijs:3000}`): empty means the service is off, another value means an external service.
- **No IP literals, no secrets in the repository.** `.env.example` only contains `change-me` placeholders. `deploy/standalone/init-env.sh` generates the secrets, and an `init` service (which Postgres and the services with secrets depend on) refuses to start with a placeholder left in.
- **Vault.** `ARIADNE_MASTER_KEY` is the only secret that must stay in the orchestrator's environment; the other integration settings live encrypted in the `integration_setting` table and are managed from the dashboard. `ariadne secrets import-env` migrates existing ones.
- **External GPU batches.** A batch run may use an external GPU notebook; the optional `gpu-gateway` is then the **only** part of Ariadne reachable from the internet, and only while a run holds a valid token. It stores results only; it does not validate or publish them.
- **Data and compute are separate.** Databases, volumes, models, ZIMs, uploads and backups belong on a data server; servers, workers and jobs run on the orchestrator hosts, which mount the data over a network file system. Do not leave data only in a local volume of one host.

---

## 14. Decisions to fix now (cheap now, expensive later)

1. **Stable ids:** `document.id = <source_id>/<path>`, with the version in a separate column. Updating Wikipedia breaks no links, vectors or statistics.
2. **Version the embedding model** in every vector (`model_id`).
3. **Origin and score of every link** (`link.kind`, `score`, `status`).
4. **Identity from day one:** every MCP/API call carries a key and maps to a `principal`. Without this there are no "who uses it" statistics.
5. **Pluggable sources:** a `Source` interface (list, read, internal links, metadata). ZIM is the first implementation; PDFs, a notes vault or others come later without touching the core.
6. **Multilingual from the start:** a multilingual embedding model and `language` on all entities.
7. **Logging designed for training:** keep complete sessions (question, calls, results, used, answer), not just counters.
8. **Queue in Postgres:** one stateful service. Redis only if proven necessary.
9. **Adapters** around MemPalace (`SemanticStore`), Wiki.js (`WikiBackend`) and embeddings (`Embedder`).

---

## 15. Final phase: an LLM trained to search

A **small** open model (1 to 3 billion parameters, with tool calling; candidates and exclusions are in [`ARCHITECTURE-TRAINING.md`](ARCHITECTURE-TRAINING.md)) that, instead of answering from memory, has learned to:

1. decide when it needs to search (almost always);
2. formulate good queries, including in other languages;
3. navigate the graph (`neighbors`, `translations`, `read`);
4. answer **citing** the service's `document_id`s;
5. say "I did not find it" instead of inventing.

**Pipeline.**

1. **SFT** on trajectories in which the model calls the Ariadne MCP, reads results and answers with citations. Sources: real logs (`query_log`/`query_result`) of strong agents using the service, and synthetic trajectories generated from articles (a question generated from an article with a known answer).
2. **RL** with Ariadne itself as the environment. Reward: plus for a correct answer **with** a valid citation and for efficient use of calls; minus for an answer without citation, a hallucination, or a citation of a document that does not support the claim.
3. **Evaluation** on questions with known answers and, in particular, on content **added after** training, which shows the model searches rather than memorizes.

**Where it trains.** An Apple-silicon workstation with MLX / mlx-lm: LoRA up to about 3B parameters, QLoRA up to about 7B. RL is slower but viable as a prototype.

**Honest limits.** What the base model already knows is not erased; it is taught to doubt it and verify. Quality depends heavily on the volume and diversity of logged trajectories, hence section 14, point 7. The loop: use, logs, trajectories, training, a model that searches better, more and better use.

---

## 16. Roadmap by phase

| Phase | Deliverable |
|---|---|
| **0: Feasibility spike** | Embeddings on a no-AVX CPU, ZIM reading with `python-libzim`, pgvector in compose; embedder chosen |
| **1: Skeleton** | Repo, compose, migrations, `principal` + keys |
| **2: MCP + fallback** | `search`/`read`/`sources` over level 1; `query_log` |
| **3: Level 3 by use** | Embedder, chunks, queue, router with thresholds, decaying `heat` |
| **4: Neighbors** | Neighbor worker and the daily budget |
| **5: Graph across ZIMs** | Article vectors, `linker`, `link` table |
| **6: Level 2** | Wiki.js importer with link blocks; selective import; curation |
| **7: Languages** | `concept`, `langlinks`/Wikidata import, `translations`, historical rule, local translation |
| **8: Web app** | Reading, graph, statistics, sources, curation, links, operations |
| **9: Rooms** | Wings and rooms behind `SemanticStore`; optional MemPalace mirror |
| **10: Courses** | Canonical Git format, Moodle + CodeRunner, Jobe |
| **11: Books** | Calibre + Kavita, chapter extraction, license classes |
| **12: Catalog and releases** | Kiwix catalog in the dashboard; backups; weekly encrypted release |
| **Final: searching LLM** | Trajectory exporter, SFT + RL in MLX, evaluation (section 15) |

Phases 0 to 12 are implemented; the final phase is planned.

---

## 17. Risks and open questions

| Risk / question | Impact | Mitigation |
|---|---|---|
| Binaries without support for CPUs without AVX | High | Canary benchmark in `bench/embeddings`; build `llama.cpp` without AVX; delegate embeddings over HTTP |
| MemPalace's pgvector backend is in preview and its package requires chromadb | Medium | Wings and rooms are Ariadne's, on its own tables; MemPalace is an optional mirror (section 4.5) |
| The curator LLM hallucinates or follows instructions in content | Medium | Only flagged and health cases; the prompt treats text as data; deterministic fidelity check; human review; no auto-publish by default |
| Leaking private content (purchased books, courses) off the data server | High | Mandatory class at intake, inherited and enforced in Postgres, `assert_exportable` on every output and `assert_may_send` on every outbound send (section 4.8); curators, translators and MCP agents receive the text they work on, so they must be local or trusted |
| Hostile EPUB/PDF (zip bombs, XML entities, hidden text) | Medium | Size and compression limits, entity-bearing XML refused, HTML allow-list, hidden text kept out of chapters and flagged, extraction in a limited subprocess |

---

## 18. Getting started guide

### 18.1 Repository layout

See [`STRUCTURE.md`](STRUCTURE.md). In short: `server/ariadne/` holds the Python core (`api/`, `mcp/`, `router/`, `sources/`, `embedder/`, `wiki/`, `workers/`, `curation/`, `courses/`, `books/`, `release/`, `translate/`, `concepts/`, `cli.py`), `server/migrations/` the Alembic migrations, `web/` the React app, `bench/` the embedding benchmark and canary, and `deploy/` the per-service build contexts.

### 18.2 Conventions

- Python 3.12, `uv`, `ruff`, `mypy` (strict in `server/ariadne`), `pytest`.
- Integration tests run against the real Postgres from the compose file (`docker compose up -d postgres`) and a **small test ZIM** (`deploy/kiwix/get-test-zim.sh`).
- Configuration through environment variables (`pydantic-settings`), with the same names as in `docker-compose.yml`.
- Every external dependency sits behind an interface (`Source`, `Embedder`, `SemanticStore`, `WikiBackend`; section 14, point 9).
- Vectors: `model_id = <repo>@<revision>`; the dimension comes from the table (section 9.4).
### 18.3 Trying it

Follow [`docs/STANDALONE.md`](docs/STANDALONE.md): generate the environment file, bring up the core with `docker compose up -d`, register a ZIM (download one from the catalog in the app, or use the small test ZIM), then try `search` and `read` over MCP with an API key created in the app.

---

## 19. Glossary

**ZIM**: the Kiwix project's compressed archive format for offline web content. **Kiwix**: the project and tools (`kiwix-serve`, `libzim`) for reading ZIM files. **Wiki.js**: an open source wiki with a GraphQL API, used as level 2. **pgvector / HNSW**: the Postgres vector extension / the graph index for approximate nearest-neighbor search. **MemPalace**: an AI memory system with wings and rooms, used here as an optional mirror. **Wing / room**: a source's namespace / a computed topic inside it. **Heat**: a usage score with time decay. **RRF**: *Reciprocal Rank Fusion*, a way to merge ranked lists. **MCP**: *Model Context Protocol*, for exposing tools to LLMs. **Principal**: an identity (person or agent) holding an API key. **Curator**: the LLM, and the review flow around it, that cleans and checks imported pages. **Batch / run**: a unit of external curation work / one execution of it. **SFT / RL / LoRA / MLX**: supervised fine-tuning / reinforcement learning / parameter-efficient fine-tuning / Apple's ML framework.

---

## 20. Public version (open source)

- **License: AGPL-3.0** (`LICENSE`). Anyone who hosts a modified version for other people must give them the source. Contributions come in under the same license, with the Developer Certificate of Origin (`Signed-off-by`).
- **This repository is generated.** The maintainer develops in a separate development repository, which remains the source of truth. At each release a tree is exported from it and published as **one commit per version**, with a fresh, clean history. This repository is not a fork or a mirror of the development one.
- **What is published:** code, images, the standalone compose, the external-GPU batch flow, curation, books, courses and documentation. **What is not:** the maintainer's own deployment (hosts, storage mounts, private registry, tunnel, internal addresses and names) and the curated data. None of that is needed to run the project elsewhere; the standalone compose is the distribution.
- **Public tree rule: locality-agnostic and free of sensitive data.** The tree contains no secrets, no IP literals of any range (`localhost`, compose service names or variables with no default IP are used instead), and no private domains, hosts, paths, accounts, personal names or emails. This covers notebooks (no outputs), images (no metadata), fixtures, snapshots and the examples in documentation. It is verified twice (in the export script and in CI) before publication, with no way around it.
- **Allowlist, not denylist:** only files listed in a manifest are exported, and a new unclassified file fails the check.
- **Public images:** each release builds and publishes the standalone compose images to a public registry (the registry name is a variable).
- **Contributions** arrive as pull requests. Once accepted they are applied in the development repository **with authorship preserved** and come back in the next export. The export refuses to publish over public-side changes that have not been imported yet.
- **This document has two versions:** the maintainer's (Portuguese, with private operating blocks) and this public one (English, summarized).
