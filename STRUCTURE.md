# Ariadne — System Structure

> **Status:** v0.5 · **Date:** 24/10/2026
>
> An overview of **everything Ariadne is as a system**, without the model training part (that is in [`ARCHITECTURE-TRAINING.md`](ARCHITECTURE-TRAINING.md)). The technical detail of the knowledge service (full SQL, parameters, calibration) remains in [`ARCHITECTURE.md`](ARCHITECTURE.md), which is the source of truth for the implementation.

---

## Index

1. What Ariadne is
2. Principles
3. Overview
4. Content intake: sources
5. Curation
6. The three content bases: articles, courses and books
7. Licence classes: open and private
8. Search and the knowledge graph
9. Translation
10. Interfaces: MCP and REST API
11. Identity, users and sessions
12. Code sandbox
13. Web application (dashboard)
14. Infrastructure and deployment
15. Data model
16. Backups and releases
17. Security
18. Status and roadmap
19. The future
20. Risks and open questions

---

## 1. What Ariadne is

Ariadne is a **large, offline, self-hosted context memory**. It brings together open knowledge (Wikipedia, Stack Exchange, official documentation, manuals, courses, books) and content of its own into an organized, linked and searchable base, and serves it:

- **to AI agents**, through a single **MCP** server (Claude, Hermes, local models, any MCP client);
- **to people**, through a web application and three reading platforms: a wiki for articles, a course platform and a book library.

This layer is useful on its own, for any model, and it is the infrastructure on which the training layer will later rest.

### Non-goals

- It is not an open web search engine: the corpus is the local library.
- It does not replace Kiwix, Wiki.js, Moodle or Calibre: it orchestrates them.
- There is no multi-tenant or SaaS mode in the first version (the data model does not prevent it).
- It does not accept content whose copying requires bypassing technical protections (DRM).

---

## 2. Principles

1. **Always offline.** Everything works without internet. External services are optional.
2. **No isolated content.** Every article, lesson and chapter must be linked to others: within the same source, across sources, across bases (article ↔ lesson ↔ book) and across languages.
3. **Each type of content in its natural structure.** Articles in a wiki, courses in a course platform, books in a library. Nothing is flattened into loose pages.
4. **Canonical formats are ours.** The platforms are presentation layers, fed from normalized and versioned formats. Switching platform loses nothing.
5. **Pay for use, not for volume.** Vector indexing, curation and import happen first where there is demand.
6. **Graceful degradation.** If a layer does not know or is down, the system falls back to the next one. The answer is never "I don't know" for lack of an index.
7. **Full traceability.** Every answer, link and curated text says where it came from (source, version, path, revision).
8. **The licence travels with the content.** Every item has a licence class that follows it through the whole system.
9. **Modest hardware first.** It runs on an old server, CPU only.
10. **Everything visible on the dashboard.** What came in, how it was handled, what is being used, and by whom.

---

## 3. Overview

```
                    ┌────────────────────────── INTAKE ───────────────────────────┐
                    │  ZIM (Kiwix)   EPUB/PDF (Calibre)   Course packages   Video  │
                    └──────────────────────────────┬───────────────────────────────┘
                                                   │  Source interface
                                                   ▼
                    ┌──────────────────────── CURATION ───────────────────────────┐
                    │ classify → convert → flags? → curator → fidelity             │
                    │ (article | course | book)     licence class assigned         │
                    └──────┬──────────────────────┬───────────────────────┬────────┘
                           ▼                      ▼                       ▼
                  ┌────────────────┐    ┌──────────────────┐    ┌──────────────────┐
                  │ ARTICLES       │    │ COURSES          │    │ BOOKS            │
                  │ Wiki.js        │    │ Git (canonical)  │    │ Calibre          │
                  │                │    │ → Moodle +       │    │ → Kavita         │
                  │                │    │   CodeRunner     │    │                  │
                  └───────┬────────┘    └────────┬─────────┘    └────────┬─────────┘
                          └──────────────┬───────┴───────────────────────┘
                                         ▼
                    ┌──────────────── KNOWLEDGE SERVICE ───────────────────────────┐
                    │ level 1 archive · level 2 curated corpus · level 3 vectors    │
                    │ search router · link graph · usage-driven indexing            │
                    │ Postgres + pgvector · work queue · workers                    │
                    └──────────────┬───────────────────────────────┬────────────────┘
                                   │ MCP                           │ REST
                         Agents (Claude, Hermes…)        Web application / dashboard
                                   │
                         LibreTranslate (PT ⇄ EN) · Code sandbox (Jobe)
```

| Part | Role | Where it runs |
|---|---|---|
| `ariadne-server` | MCP, REST API, search router, call log | data server / hypervisor |
| `ariadne worker` | Indexing, neighbours, decay, import, links, curation | data server / hypervisor |
| Postgres + pgvector | Central state: sources, documents, vectors, links, queues, logs | data server |
| Kiwix (`kiwix-serve`) | Level 1: reading and text search in the ZIM files | data server |
| Wiki.js | Curated article base | data server |
| Moodle + CodeRunner | Course base, with automatic grading of exercises | data server |
| Jobe | Isolated code execution server (CodeRunner and the general sandbox) | hypervisor |
| Calibre + Kavita | Book base and web reader | data server |
| LibreTranslate | Local translation | production environment (already exists) |

---

## 4. Content intake: sources

All intake goes through the **`Source`** interface (list, read, internal links, metadata). Each type of source is an implementation; the core does not change when a new type appears.

| Source type | Examples | Status |
|---|---|---|
| **ZIM (Kiwix)** | Wikipedia, Stack Exchange, DevDocs, freeCodeCamp, LibreTexts, manuals | Implemented (phases 1–5) |
| **Books (EPUB, PDF, MOBI)** | Open books; books bought without DRM (Manning, Pragmatic, No Starch, Leanpub, Humble Bundle) | Implemented (Phase 11): EPUB and PDF by chapter; MOBI converted to EPUB by Calibre; DRM refused |
| **Course packages** | SCORM, IMS Common Cartridge, exports from platforms that allow download | Planned |
| **Video** | Own lessons or lessons the user has rights to; course videos in the ZIM files | Planned (transcription) |

### How ZIM files come in

- **Manual:** Ariadne reads the ZIM folder (`ZIM_DIR`; on the data server, a folder of the services dataset) and registers new files in `source` at startup or with `ariadne sources sync`. This still works: any `.zim` placed in the folder is served by kiwix and registered.
- **Through the dashboard (Phase 12, done):** the *Sources* module browses the Kiwix OPDS catalogue (`KIWIX_CATALOG_URL`, by default `library.kiwix.org`) with search, language, category and pagination, and administrators download a ZIM with progress. The download (worker `catalog`, job `catalog_download`) goes to a `.part` file in the ZIM folder, resumes with `Range` if the connection drops or the worker stops, checks the size and the sha-256 from the catalogue's `.meta4` (**without a sha-256 it refuses**: mirrors are third-party; an administrator may allow a download without a checksum, on purpose), and only then takes its final name; afterwards `sources sync` registers it and kiwix-serve picks it up by itself (the kiwix entrypoint keeps the `library.xml`; see 14.3). Every network hop is verified and the **validated IP stays pinned to the connection** (http(s) only; never private addresses, unless the catalogue is local; no DNS rebinding), there is a disk-space margin, a server that sends more bytes than the `.meta4` promised aborts at once, and a download can be cancelled midway. Full disk, insufficient space and missing checksum are permanent errors; `.part` files that were cancelled, failed for good or abandoned for 3 days are deleted. The same book is never downloaded twice (also while the job is running). Removing a source **archives it** (the file goes to `.archive/` and kiwix stops serving it; `restore` puts it back); the file is only deleted with explicit confirmation (the source id repeated).

### Classification at intake

Each source, or each section of a source, is classified as **article**, **course** or **book**, and goes to the matching base:

| Class | Criterion | Destination |
|---|---|---|
| Article | Reference content with no learning path (encyclopaedias, Q&A, documentation) | Wiki.js |
| Course | Has a path (modules and lessons) and, usually, exercises or tests | Moodle |
| Book | Work with chapters, read continuously, with no gradable exercises | Calibre / Kavita |

Boundary rule for the in-between cases (Wikibooks, LibreTexts, technical books): **with exercises → course; reference only → book**.

**Status (Phase 10):** done for ZIM files. The class is decided from the ZIM metadata (freeCodeCamp, Khan and Wikiversity are courses; Wikibooks, Gutenberg and LibreTexts are books; the rest are articles), corrected by hand with `SOURCE_CLASS_OVERRIDES` (per source or per section) and stored in `source.meta`; `document.content_type` is `article`, `lesson` or `chapter`, and `search`/`read` return it. A ZIM of type `course` stays only as an archive: what is searched and read are the lessons of the extracted course. `ariadne sources classify` shows the result.

---

## 5. Curation

Original content is **raw material**. Everything that is searched or served first goes through a curation pipeline that cleans, normalizes and records provenance. Protection against hidden instructions and harmful content happens **at intake**, so that nothing downstream has to defend itself.

### Hybrid pipeline

1. **Extraction** of the item from the source.
2. **Classification:** article, course or book.
3. **Deterministic conversion:** HTML to Markdown, removal of menus, footers and junk, with no model at all. For courses, extraction of the structure (modules, lessons, exercises, tests, solutions); for books, extraction by chapter.
4. **Flag triage.** Only what triggers flags goes to the curator:
   - injection patterns ("ignore previous instructions", system tags, hidden text);
   - long encoded blocks (e.g. base64);
   - dangerous commands in code (`curl | sh`, `rm -rf`);
   - clinical doses and values.
5. **Always curated:** all health content.
6. **Random sampling:** a small percentage of what triggers no flags also goes to the curator, to tune the rules.
7. **Curator:** a larger model with good judgement evaluates, cleans and rewrites in a standard format. It runs in batches, when the machine is free.
8. **Fidelity check:** the facts in the original and in the rewrite must match; if a number or statement has changed, the item does not pass.
9. **Publication** to the matching base, with provenance (source, version, path), licence class and origin attribution (e.g. CC BY-SA).
10. **Human review queue** for doubtful cases.

Curation follows selective import: **what is used is curated first**.

**Where the LLM work runs.** By default, on the **Mac Studio** (the "local" target: `llama-server` with Metal through a launchd service, `deploy/mac-curator/`; the production environment's server has neither AVX nor a usable GPU). With the Mac off, jobs wait in the queue without being lost, and the panel shows "Mac offline". The *Curation* panel of the dashboard chooses the target (local, free Kaggle GPU, manual Colab; a paid service is planned), offers play/pause, schedules an hourly window, and shows progress and the GPU hours of the week. What comes back from an external GPU goes through the same validation (schema, fidelity) and **always stays for human review**, with a batch review screen; whatever the GPU does not consume passes to the local curator. Design and limits in [`docs/WIKI-IMPORT.md`](docs/WIKI-IMPORT.md), section 7.

### Updates

- When a new version of a source arrives, each item is compared with the previous one by a fingerprint of its cleaned text.
- Unchanged items stay. Changed items go back through the pipeline and produce a new revision. Removed items are archived, never deleted.
- The old version of the source is kept for a transition period.
- Citations point to exact revisions, so they remain valid after updates.

---

## 6. The three content bases: articles, courses and books

### 6.1 Articles — Wiki.js

- A Wiki.js instance with its own database on the same Postgres.
- Wiki.js is not a copy: it is a **curated and linked re-reading** of the sources, with "Links" blocks (other sources, other languages, related lessons and chapters).
- Paths: `/<locale>/<domain>/<subdomain>/<title-slug>` (e.g. `/en/health/medicines/paracetamol`), the locale being the page language and the domain tree being the one in `ariadne/wiki/domains.yaml` (see `docs/WIKI-IMPORT.md`).
- Selective and incremental import, behind the `WikiBackend` interface.
- The Wiki.js revision history is what makes citations stable.

### 6.2 Courses — canonical format + Moodle with CodeRunner

**Canonical format (ours, in Git).** Courses are stored in a normalized, versioned format; Moodle is fed from it.

- **Hierarchy:** course → modules → lessons → blocks.
- **Block types:** text, code example, worked example, exercise with tests, quiz, solution, video with transcript.
- **Metadata:** level, prerequisites, learning objectives, language (in programming courses), source of origin, licence and attribution, licence class.
- **Versioning:** each course has its own version and each change is a commit. A new version of the source produces a new version of the course, without deleting the previous one.
- **Prerequisites as a graph:** a link type of its own, "requires", between lessons and between courses.

**Platform: Moodle + CodeRunner.**

- Moodle: mature, with a web services API and package import (Common Cartridge, SCORM, Moodle XML for questions).
- **CodeRunner:** automatically grades programming exercises with tests, with no AI model judging. It runs the code on the **Jobe** server (section 12).
- Moodle's database on the same Postgres (its own database).
- Publication is done through an adapter (`CourseBackend`), just like `WikiBackend`: the canonical format is the source of truth.

First course sources: freeCodeCamp (challenges with ready-made tests), LibreTexts, Wikiversity, open books on algorithms and programming with exercises.

**Status (Phase 10, done):**

- **Canonical format** in its own Git repository (`COURSES_REPO`; the code repository carries no content): `<course>/<version>/course.yaml` and one folder per module with one YAML per lesson. Pydantic schema validated (ids, solutions that point to exercises, prerequisites without cycles), Markdown and code in readable blocks, exact round trip, **one commit per change**, one folder per version (a new version of the source never deletes the previous one). Details: `ARCHITECTURE.md`, section 4.7.
- **freeCodeCamp extraction** (`ariadne courses import-fcc`): each challenge is a lesson with text, exercise (starter code, tests) and solution. Verified with the real ZIM files `freecodecamp_en_coding-interview-prep` (6.4 MiB, 56 lessons, 304 tests) and `…javascript-algorithms-and-data-structures` (299 lessons, 1381 tests). freeCodeCamp's `assert` tests run on Jobe's Node through a subset of chai (a shim); HTML/CSS challenges, projects and modules, and tests that use the DOM, `expect`/`should` or `spyOn`, **are not mapped** (they stay in the canonical format, with the reason, and do not go to Moodle): 85 of 1381 in the JavaScript ZIM, 0 of 304 in the interview one. Each exercise's official solution confirms the mapping on Jobe (`--verify`).
- **Moodle 4.5 LTS + CodeRunner** (+ `qbehaviour_adaptive_adapted_for_coderunner`) in the compose file, with its own database on Postgres and a local plugin (`local_ariadne`) that publishes the course: one section per module, one page per text lesson, and one quiz per lesson with an exercise (one CodeRunner question per exercise) or with `quiz` blocks (multiple-choice questions). The `CourseBackend` (`MoodleBackend`) is idempotent (it only updates what changed). Each version of the course is a Moodle course.
- **Lesson curation:** the Phase 6 triage (flags, health, sampling) runs over the text and code of each lesson; a lesson with flags is held until the curator or human review approves it. The curator's rewrite is not written back into the blocks.
- **Graph:** the directed `requires` edge links lessons and courses; freeCodeCamp declares no prerequisites, so the curriculum order within each module is used. Lessons are documents (search, `read`, `neighbors`) and the linker connects them to articles by title and by vector.
- **App:** the **Courses** module (versions, publication status, lessons, extracted exercises and tests, history).
- **To do:** rewrite the block text with the curator's result; extractors for LibreTexts, Wikiversity and course packages (SCORM, Common Cartridge); extraction of quizzes (`# --questions--`) and of freeCodeCamp videos (the schema and the Moodle publication already support them, but the small ZIM files do not have them and there is no real example to validate the extractor).

### 6.3 Books — Calibre + Kavita

- **Calibre** as the canonical base of the library: management, metadata and conversion between formats (EPUB, PDF, MOBI). Only the command line is used (`calibredb`, `ebook-convert`), in its own container behind an HTTP agent with an API key; it is the only component that writes to the library folder.
- **Kavita** as the self-hosted web reader, with API and OPDS, over the same folder (read-only). Ariadne results carry the book's URL in Kavita.
- Ariadne extracts the text by chapter for indexing and search, with citations by book, chapter and position.
- Its own adapter (`LibraryBackend`, with Calibre as the first implementation).

**Status (Phase 11, done):**

- **Intake** (`ariadne books add FILE --license-class open|private --license-name …`): the licence class and the licence are mandatory, with no default value. DRM is refused **before** Calibre sees the file: EPUB with Adobe ADEPT, FairPlay or encrypted content, encrypted PDF and encrypted MOBI/Kindle (EPUB font obfuscation does not count). The same file cannot enter twice (sha256 fingerprint). MOBI is converted to EPUB in Calibre.
- **Extraction by chapter** (`ariadne/books/extract.py`, with no native dependencies: EPUB via zip + bs4, PDF via `pypdf`): in EPUB, the index (`nav` or NCX) with anchors inside the files; in PDF, the bookmarks, otherwise the "Chapter N" headings at the top of pages, otherwise windows of 10 pages; the note on how it was split is kept in the book. HTML goes through the same allowlist as the lessons, and text hidden by CSS is removed from the chapter (and kept as a signal for curation). Each chapter is a document (`content_type='chapter'`, `book:<book>/<NNN-title>`).
- **Citation by book, chapter and position**: `book:<book>/<chapter>#<position>`, where the position is the character offset in the text that `read` returns; `read(document_id, offset)` continues reading from there.
- **Search:** level 2 by text (`chapter.search_tsv`, a `tsvector` generated over the extracted text, chosen instead of Kavita's index because that only searches metadata and the positions have to match the text returned by `read`); level 3 by use, like the articles (`ariadne books index` already requests it); graph: the linker connects chapters to articles and lessons by title and by vector.
- **Chapter curation** (flags, health, sampling, curator, fidelity): since a book has no platform that publishes it chapter by chapter, the decision is a signal: a chapter with flags stays searchable but is marked; a chapter rejected in human review stops appearing in results.
- **App**: the **Books** module (library, extracted chapters, licence class, open in Kavita) and the chapter panel in the reader. Details: `ARCHITECTURE.md`, section 4.8.

### 6.4 Links between bases

The three bases are part of the same graph: an article points to the lessons where the topic is taught and to the chapters where it is covered in depth; a lesson points to the reference articles; a chapter points to related articles and lessons.

**Status (Phase 11):** chapters are documents like any other, so `neighbors`, the linker (title and vector) and semantic neighbourhood link articles, lessons and chapters. Wiki.js pages only point to **open** chapters (to the book in Kavita); links to private content are visible in the app, in MCP and in `neighbors`, never on a page that goes out in the releases.

---

## 7. Licence classes: open and private

Each item receives a **licence class** at intake. The class follows the item and everything derived from it (pages, lessons, chapters, chunks, vectors, links, logs), and the system blocks any leak.

| Class | What it includes | May go to public releases | Local search |
|---|---|---|---|
| **open** | ZIM files, CC content, public domain | Yes (with attribution) | Yes |
| **private** | Purchased content; own content the user does not want to publish | **Never** | Yes |

### Rules for purchased or own content

- Only purchases from **platforms that allow downloading a full personal copy**, without technical protections, are accepted. Kindle books and other DRM-protected books are **not** supported: circumventing DRM breaches the terms of use and, in the EU, is illegal.
- **Content downloaded or produced by the user themselves**, where they hold the rights (for example, their own lessons), is accepted.
- Buying gives the right to read, not to redistribute: nothing of the private class, nor anything derived from it, leaves the data server.

---

### How it is guaranteed (Phase 11, done)

- **Where the class lives**: `source.license_class` and `document.license_class` (`open` | `private`, `NOT NULL`, with no default value). ZIM files are open; courses declare it in the canonical format (a mandatory field); books declare it with `books add --license-class`. A document inherits its source's class (`upsert_documents`), and Postgres prevents an open document in a private source (triggers `trg_document_license` and `trg_source_license`); chunks, vectors, links and query logs inherit it **by join** with the document, without duplicating the column (there is a partial index for private items only, so checking is cheap).
- **Checks on outputs**: `ariadne.licenses.assert_exportable(...)` is the single function; it refuses sources, documents, chunks (and their vectors) and links (private if either end is private) that are private or derived from private content, and logs the block (`license_block`). Today it guards: vector export (`ariadne vectors build --out`, which **refuses**), the MemPalace mirror and the "Links" block of the Wiki.js pages (which **filter** with `document_is_open()`); the external release of Phase 12 (`ariadne release`, section 16) filters and then calls it over everything it carries (if anything private gets through, it refuses and nothing is written), and so does any new export. Local search (MCP, API, app) sees both classes.
- **Changing class** is always explicit (`ariadne books set-license`, `PUT /api/books/{id}/license`); making a book open asks for confirmation, because it asserts that the book may be redistributed.
- **Licences module** in the app: the class of each source and each item, the saved outputs and the blocks applied.
- **Sending to services** (`licenses.assert_may_send`): private text, or text derived from private text, only goes to a service whose host is on an explicit local list, `PRIVATE_ALLOWED_HOSTS` (empty = the compose services and localhost; `*.home` works for a suffix). This applies to the **curator** (`CURATOR_URL`), to **external GPU batches** (Kaggle/Colab: they never take private texts), to **HTTP embeddings** (`EMBEDDER=http|llamacpp`: neither indexing, nor the article vector, nor search re-scoring), to the **translator** (with `document_id`), to **Moodle publication**, to the import into **Wiki.js** and to **Jobe** (`courses verify`). Outside the list nothing is queued or dispatched: the item goes to human review (curator), the job fails permanently (embeddings, Wiki.js) or the request is refused (Moodle, Jobe), and it is logged in `license_block`. Whoever puts the curator on the Mac (`host.docker.internal`) adds it to the list, on purpose. **Limits**: a text sent to the translator without a `document_id` does not know where it came from; and the MCP agent that reads a private chapter receives the text: whoever connects a remote agent to MCP decides what it sees.

---

## 8. Search and the knowledge graph

### 8.1 Three levels

| Level | What | Technology |
|---|---|---|
| **1 — Archive** | Original sources, text search | Kiwix (`kiwix-serve`, `python-libzim`) |
| **2 — Curated corpus** | Curated and linked articles, lessons and chapters | Wiki.js, Moodle, Calibre |
| **3 — Semantic** | Vectors of what is used and of its neighbours | Postgres + pgvector (HNSW) |

### 8.2 Search router

1. The question is vectorized on the CPU (one sentence, a few milliseconds).
2. Search in pgvector: if the best result is very similar (`τ_high = 0.86`), it answers from level 3 alone.
3. Grey zone (`0.80` to `0.86`): merges level 3 and text search (RRF).
4. Below that: text search only, in levels 1 and 2.
5. Everything that comes from text search enters the indexing queue.
6. If the embedder fails, search degrades to level 1 with a warning.
7. For **historical** questions, the version in the original language of the concept enters the results with the "original source" badge (section 8.5).
8. With LibreTranslate configured, a question in PT or EN is also searched translated in the sources of the other language, merged by RRF (section 9).

The thresholds were calibrated on the current model and are recalibrated when there are logs with feedback (detail in `ARCHITECTURE.md`, section 5.1).

### 8.3 Usage-driven indexing

The vector index is a **semantic cache that warms up with use**:

- Each document has a **heat** with decay (half-life of 30 days): +0.3 per result shown, +1 per result used, +2 per positive feedback.
- When a document passes the threshold (heat 5), the `neighbors` worker queues its neighbours, one hop away: adjacency within the article itself, the source's internal links, cross-links, same room, semantic neighbours.
- **Daily budget** of vectorized chunks (20 000 by default). What does not fit in the day waits for the next day, without being lost.
- Cold start: seed chosen collections with `ariadne index enqueue`.

### 8.4 Link graph

- Links **within** each source (the ZIM's internal links) and **between** sources (equal titles and entities, Wikidata, vector similarity between article vectors, manual links).
- Links **between bases** (article ↔ lesson ↔ chapter) and **prerequisite** links between lessons.
- Each link records its type, score, the model that generated it and its state (proposed, accepted, rejected). Doubtful vector links stay as proposals until validated.

### 8.5 Links between languages

- **Multilingual** embedding model (`intfloat/multilingual-e5-small`): level 3 already crosses languages.
- Exact linking via Wikidata *sitelinks* or *interlanguage links* (`langlinks` dumps), imported offline: `ariadne concepts import-sitelinks` and `import-langlinks`. Only articles that exist in the ZIM files present are imported; the import runs in batches and is idempotent.
- A **concepts** table groups the versions of the same topic (Wikidata QID when one exists); the `langlink` edges (same project, another language) and `wikidata` edges (another project) are kept in the graph, visible in `neighbors`.
- The **original language** of a concept is set by hand (curation) or from a TSV of countries; without data it stays undefined, and is never invented. The `translations` tool lists the versions in other languages and marks the original.
- For historical topics (a simple, explainable rule: keywords, years, dates), the version in the **original language** of the event enters the results with the "original source" badge; the Wiki.js page has an "Other languages" section with the same badge.

Details, how to obtain the dumps and the limits: `ARCHITECTURE.md`, section 8.

### 8.6 Organization by wings and rooms

A logical hierarchy inspired by MemPalace: **wing** = source, **room** = topic within the wing. Rooms are **computed**: a k-means over the article vectors of each wing (~√(n/2) rooms, between 8 and 200), labelled by the three most central titles, with stable ids across recalculations; the `rooms` worker assigns new articles to the nearest room and recalculates the wing when it grows by 25%. They are used to filter search (`wings`, `rooms`), to give a small merge bonus to the results in the rooms closest to the question, and for neighbourhood. They appear in `sources`, in the `rooms` tool, in `GET /api/rooms` and in the web app (room in the reader, room filter in search).

MemPalace itself is **optional** (`ariadne[mempalace]`, `SEMANTIC_STORE=mempalace`): the package requires chromadb and stores the vectors in a table of its own, so Ariadne's tables are the source of truth and the adapter behind `SemanticStore` only mirrors, in one direction (Ariadne → MemPalace), the chunks with their wing and room. Without the extra, the server starts with pgvector and says why in `/health`. Details and the decision: `ARCHITECTURE.md`, section 4.5.

---

## 9. Translation

- **LibreTranslate** service, local and offline (never a public service: the text of questions and articles goes there, and nothing private may leave the data server): either the one that already exists in the production environment (`LIBRETRANSLATE_URL`) or the one in the compose file, optional (`docker compose --profile translate up -d`). Behind the `Translator` interface; without a URL, translation is disabled and the rest keeps working.
- Translates **questions** (a PT or EN question is also searched in the other language; a result that only it found is marked `translated_query`) and **texts** (`translate` tool and `POST /api/translate`, so the app can offer translated versions).
- Code (blocks and inline), `[doc:…]` citations, URLs, function names and identifiers, and a configurable list of technical terms are replaced with placeholders before translation and restored afterwards; if a placeholder is lost, the translation is refused rather than returning broken code.
- Translations are cached. Service failures degrade without error (original text and a warning).
- Search always also queries the sources in the original language of the question.
- LibreTranslate's models (Argos) lean towards PT-BR; for PT-PT a dedicated model is loaded and `LIBRETRANSLATE_PT_CODE` points to it (`ARCHITECTURE.md`, section 8.7).

---

## 10. Interfaces: MCP and REST API

### 10.1 MCP

Endpoint `http://<machine>:8800/mcp` (Streamable HTTP), with an API key per principal; `ariadne mcp-stdio` for clients that only speak stdio.

| Tool | Purpose | Status |
|---|---|---|
| `search` | Search across the three levels, with source, level, score and `document_id` | Implemented |
| `read` | Clean text of a document or section, with links | Implemented |
| `sources` | Available sources | Implemented |
| `feedback` | Mark whether a result helped | Implemented |
| `neighbors` | Linked documents | Implemented |
| `translations` | Versions in other languages, with the original-language mark | Implemented |
| `translate` | Translates a text (code and citations intact) | Implemented |

`search`, `read` and `neighbors` also cover **lessons** (Phase 10: `content_type`, the lesson data and the `requires` prerequisites) and **book chapters** (Phase 11: `book`, `position`, `citation` and `reader_url` in the results; `read` accepts `offset` and returns the book with the previous and next chapter). Every call is logged (`tool_call`), and every search in `query_log` / `query_result`.

### 10.2 REST API

| Endpoint | Status |
|---|---|
| `GET /api/search`, `GET /api/documents/{id}`, `GET /api/sources`, `GET /api/whoami` | Implemented |
| `GET /api/jobs` (queues and daily budget) | Implemented |
| `GET /api/neighbors`, `GET/POST /api/links` | Implemented |
| `GET /api/translations`, `GET/POST /api/concepts/{qid}` (original language, `user` only), `POST /api/translate` | Implemented |
| `GET /api/curation`, `GET /api/curation/item`, `POST /api/curation/review` (`user` only) | Implemented |
| `GET /api/courses`, `GET /api/courses/{id}`, `GET /api/courses/{id}/exercises` (any principal); `POST /api/courses/{id}/publish`, `POST /api/courses/sync` (administrators only) | Implemented (Phase 10) |
| `GET /api/books`, `GET /api/books/{id}` (any principal); `POST /api/books/sync`, `POST /api/books/{id}/index`, `PUT /api/books/{id}/license` (administrators only) | Implemented (Phase 11) |
| `GET /api/licenses`, `GET /api/licenses/documents`, `GET /api/licenses/blocks` (class of each source and item, saved outputs, blocks) | Implemented (Phase 11) |
| `GET /api/stats/{overview,top-documents,principals,coverage}` (usage, who uses it, coverage) | Implemented |
| `POST /api/jobs/{id}/retry`, `GET /api/config`, `GET/POST /api/collections` | Implemented |
| `GET /api/catalog` (search, language, category, pagination; shows what is already installed or has a newer version), `GET /api/catalog/filters`, `GET /api/catalog/downloads` (any principal); `POST /api/catalog/{book}/download` (`?allow_unverified=true`: without the catalogue's sha-256, by explicit decision), `DELETE /api/catalog/downloads/{job}` (cancel), `DELETE /api/sources/{id}` (archive; `?delete_file=true&confirm=<id>` deletes the file), `POST /api/sources/{id}/restore` (administrators only) | Implemented (Phase 12) |
| `GET /api/backups` (last backup and release, size, restore rehearsal, next schedule) for any principal; `POST /api/backups/release` (request a release now) and `POST /api/backups/{id}/rehearsal` (record a rehearsal done outside) for administrators only; agents do not see the error or who asked | Implemented (Phase 12) |
| `GET/POST /api/principals`, `PATCH/DELETE /api/principals/{id}` (administrators only) | Implemented (the first administrator key, via the CLI) |

OpenAPI generated by FastAPI at `/docs`.

---

## 11. Identity, users and sessions

- **Identity from day 1:** every MCP and REST call carries an API key that identifies a **principal** (`user` or `agent`). Creation: in the app (Operations, administrators only) or `ariadne principal create --kind user --role admin` for the first one.
- **Roles:** **administrator** (`principal.role = admin`: manages principals and keys; highest priority) and **users** (`member`; about three at the same time). Only a `user` principal can be an administrator; revoking a key deletes the hash and the principal stays in the history; the last administrator is never revoked. Agents only read (curation and links require a `user`).
- **Sessions stored in Postgres:** the harness (Hermes, Claude or another) only sends the session identifier; the context lives in the system, so changing the subject or the client loses nothing.
- **User data** (sessions, keys, query logs) always stays on the data server only.
- **Secrets and integrations (the vault):** the keys and options of the integrations (Wiki.js, Calibre, Kavita, Moodle, Jobe, curator, Kaggle, LibreTranslate, GitHub…) are stored in the database (`integration_setting`), **encrypted** with AES-256-GCM (the setting's name is included as associated data), and are managed on the **Integrations** screen of the dashboard (administrators only): set yes/no, origin (vault or environment), last change, edit/rotate/delete, "test connection", and generating a random value when the secret is ours. The **master key** is the only variable kept in the orchestrator (`ARIADNE_MASTER_KEY`). A value in the vault wins over the environment variable; without a value in the vault the variable applies (compatible with the previous behaviour). The server and the worker reread the vault every few seconds (`VAULT_CACHE_SECONDS`) and rebuild the affected clients, without restarting. No API response nor log line carries a secret in clear text (at most the last 4 characters; only *generating/rotating* one of our secrets shows the new value, once); `integration_audit` records who changed what and when, without the value. With the master key missing or wrong, the vault switches off and the variables apply (the dashboard warns); losing it loses the vault's values. `ariadne secrets import-env` copies into the vault what is currently in the environment. Ariadne **does not write** to the data server: shared secrets (Calibre, Jobe) are carried over by hand (the operator's own deployment notes).

---

## 12. Code sandbox

- **Jobe** as the isolated execution server, used by Moodle's CodeRunner to grade exercises and reused as the system's general sandbox.
- Initial languages: Python, JavaScript (Node), Go; front-end with a headless browser (Playwright). **Status (Phase 10):** the Jobe in the `trampgeek/jobeinabox` image ships Python, Node, C, C++, Java, PHP, Octave and Pascal, **without Go** (still to be solved); the Playwright front-end is not done.
- Each execution: disposable container, no network, read-only disk with a temporary folder, hard limits on time, memory and processes.
- Runs on a separate machine (hypervisor), away from the central state. **Status (Phase 10):** the compose file brings Jobe only for development: on an `internal: true` network (no exit to the internet), behind an nginx (`jobe-gateway`: API key, request limit per client, REST API only) that is the only bridge to the network of Moodle, the server and the worker (fixed subnet and gateway IP, so that Moodle blocks the other private networks), with a read-only disk, reduced capabilities, process and memory limits, and the students' code with no network at all (neither the gateway nor the loopback). On the hypervisor, the firewall only accepts the data server's IP. On the data server it leaves the compose file and goes to the hypervisor (`JOBE_URL`; in Moodle `JOBE_HOST`). The client `ariadne/sandbox/jobe.py` serves CodeRunner, the verification of solutions (`ariadne sandbox run`, `courses verify`) and any future use; the MCP still has no execution tool.

---

## 13. Web application (dashboard)

React + Vite + TypeScript, served by `ariadne-server` at `http://<machine>:8800/` (Phase 8, done; TypeScript client generated from the OpenAPI, login with a user's API key). Details in `ARCHITECTURE.md`, section 12.

| Module | What it shows and allows | Status |
|---|---|---|
| **Reading** | Unified search; reader with a links panel (by source and by language), translations and the "original source" badge | Done (articles, lessons and chapters, with citation and a link to Kavita) |
| **Graph** | Neighbourhood of an item, filterable by link type | Done |
| **Statistics** | Queries per day, level that answered, p50/p95 latency, hottest items, who uses it most, coverage by source | Done (hottest rooms: postponed) |
| **Sources and catalog** | Registered ZIM files, class (articles, courses, books), space used, import and curation status; **Kiwix catalogue** (search, language, category, installed / newer version available), downloads with progress and estimate, cancel; archive, restore and delete ZIM (administrators) | Done (Phase 12), with the licence class of each source; course packages are for later |
| **Curation** | Status of each item, flags triggered, curator decision, fidelity check, human review queue (original and curated side by side), link to the original; **control of the compute targets** (play/pause, target, window, GPU hours) and **batch review** of results coming from external GPUs | Done |
| **Courses** | Courses and versions, Moodle publication status, extracted exercises and tests | Done (Phase 10) |
| **Books** | Library, extracted chapters, licence class | Done (Phase 11) |
| **Links** | Accept or reject proposals, create manual links, collections to pre-warm, original language of topics | Done |
| **Operations** | Queues (retry failed), daily budget, service health, configuration in effect (read-only), API keys, users and roles; **backups and releases**: last local backup, last release (size, parts, published?), status of the restore rehearsal (and whether it is overdue), next schedule and "build release now" | Done (backups and releases: Phase 12) |
| **Licenses** | Licence class of each source and item; blocks applied | Done (Phase 11) |

---

## 14. Infrastructure and deployment

### 14.1 Machines

| Machine | Role |
|---|---|
| **data server** (2× Xeon X5550, 128 GB RAM, AMD RX 540) | Central state and services: Postgres, Ariadne, Kiwix, Wiki.js, Moodle, Calibre/Kavita. Backups. |
| **hypervisor** | Services that need isolation or spare capacity: Jobe, possibly parts of the control plane |
| **Mac Studio** (M2 Max, 32 GB) | Development, heavy offline workloads (initial embeddings) and the **LLM curator** (Gemma 3 12B Q4_K_M on `llama-server` with Metal, launchd service, API-key protected only; section 5 and `deploy/mac-curator/`) |
| **Existing production environment** | LibreTranslate |

**Critical constraint:** the Xeon X5550 processors have no AVX, AVX2, FMA, F16C or BMI2 (only SSE4.2). No native dependency may assume these extensions; the `bench/embeddings` benchmark serves as the canary. The GPU has no ROCm: everything works on the CPU only.

### 14.2 Embeddings

- Model: `intfloat/multilingual-e5-small` (384 dimensions), ONNX on CPU, fp32 or int8.
- The heavy initial load is generated on the Mac and imported.
- `Embedder` interface with implementations for ONNX, llama.cpp (compiled without AVX) and HTTP.
- Each vector stores `model_id = <repo>@<revision>`; switching model generates new vectors side by side.
- A **remote** embedder (`EMBEDDER=http|llamacpp` with `EMBEDDER_URL` outside `PRIVATE_ALLOWED_HOSTS`) never receives text of the private class (section 7): private chapters and lessons are not indexed nor included in the search re-scoring; the local ONNX and the compose services serve everything.

### 14.3 Docker Compose

A `docker compose up -d` brings up the stack; on the data server it runs as a custom app.

| Service | Status |
|---|---|
| `postgres` (pgvector, Postgres 17; databases `ariadne`, `wikijs`, `moodle`) | Implemented (the `moodle` database is created automatically, also on older volumes) |
| `kiwix` (`--library --monitorLibrary` over a `library.xml` that the entrypoint keeps equal to the ZIM folder, read-only; see below) | Implemented (Phase 12) |
| `server` (MCP + REST + app) | Implemented |
| `worker` (indexer, decay, neighbors, linker, importer, curator, rooms, mempalace, courses, books) | Implemented |
| `worker-heavy` (`--only catalog,release`: ZIM downloads and the weekly release, which encrypts gigabytes, separate from indexing) | Implemented (Phase 12) |
| `wikijs` | Implemented |
| `moodle` + CodeRunner plugin (Moodle 4.5 LTS, `local_ariadne`) | Implemented (Phase 10) |
| `jobe` + `jobe-gateway` (internal network; on the data server it moves to the hypervisor) | Implemented (Phase 10) |
| `calibre` (CLI + HTTP agent with key) + `kavita` (0.9, web reader) | Implemented (Phase 11) |
| `libretranslate` (optional, `translate` profile; by default uses the existing one) | Implemented |

**kiwix and the `library.xml` (Phase 12).** kiwix-serve only sees the ZIM files that existed when it started, so it now runs with `--library --monitorLibrary /library/library.xml`. We chose to keep the `library.xml` **inside the kiwix container itself**, with the `kiwix-manage` tool that the image already ships: an entrypoint (`deploy/kiwix/entrypoint.sh`) scans the ZIM folder every 5 seconds, adds the new ones (`kiwix-manage add`) and removes the ones that have disappeared (`remove`). It needs no auxiliary service nor new binaries in the worker, and the ZIM files remain mounted **read-only**: the only writable part is `/library`, a tmpfs (rebuilt from scratch at every start, so there is no state to lose and no file with the wrong owner). Book names are kept (the file name without `.zim`). A downloaded ZIM is served within a few seconds.

**Server/worker image.** Besides `git`, it carries `age` (Debian package, Go binary), `ssh-keygen` (openssh-client, signs the release manifest) and `pg_dump` 17 (from the PGDG repository: Debian's is older than the server). The new volumes: `BACKUP_DIR` (`./data/backups`, mounted at `/backups`, in the server and the worker; on the data server it must belong to user 1000), and the ZIM folder must now be writable by the worker (user 1000).

---

## 15. Data model

Postgres is the **only stateful service** (the queue included, with `SKIP LOCKED`; no Redis). The full SQL is in `ARCHITECTURE.md`, section 10.

| Table | Role |
|---|---|
| `source` | Registered sources (type, language, version, path, wing) |
| `document` | Items with a stable ID `<source_id>/<path>`, version, heat, room, link to the curated page |
| `concept` | Groups the same topic across several languages (Wikidata QID) and stores the original language |
| `chunk`, `chunk_vector` | Level 3: text pieces and their vectors (HNSW) |
| `document_vector` | One vector per article, for links and neighbourhood |
| `link` | Graph edges, auditable |
| `principal` | Users and agents, with API key |
| `query_log`, `query_result`, `tool_call` | Log of searches, results and calls |
| `job` | Generic work queue (priority, attempts, postponements, deduplication) |
| `translation_cache` | Translations already made (key = hash of engine, languages, terms and text) |
| `backup_run` | Local backups and releases (Phase 12): status, size, parts, fingerprints, publication and restore rehearsal; no content |

**To add with this version of the structure:**

- ~~`content_type` in `document` (`article`, `lesson`, `chapter`)~~ — done (migration 0010). ~~`license_class` (`open`, `private`) in `source` and `document`~~ — done (migration 0011: `NOT NULL`, with no default value, inherited by everything derived from them through a join with the document, with Postgres triggers as a safety net); `license_block` records what the outputs refused or filtered.
- ~~Index tables for courses (`course`, `course_version`, `lesson`, `exercise`)~~ — done (migration 0010), pointing to the canonical format (Git commit) and to Moodle (course, activity, question). ~~Books (`book`, `chapter`)~~ — done (migration 0011): `book` (metadata, file in Calibre, class, where it is in Kavita) and `chapter` (one per chapter, with the extracted text and the `tsvector` generated for search).
- ~~Curation status per item~~ — done (`curation` table, migration 0006: flags, decision, fidelity, human review).
- ~~`requires` link type (prerequisite)~~ — done (directed edge, between lessons and between courses); the article ↔ lesson links come from the linker (title and vector).

---

## 16. Backups and releases

**Status: done (Phase 12).**

- **Everything lives on the data server**, covered by the data server's own ZFS snapshots (Ariadne does not touch them). Recommended policy: one dataset per type of data, so that each can have its own retention: `pgdata` and `backups` (what cannot be recreated: snapshots hourly for 1 day, daily for 30, weekly for 8, monthly for 12, replicated to another site **on your own network**), `courses` (same, it is the course source of truth), `books` and `kavita-config` (daily and weekly), `zim` and `models` (only weekly and short-lived: they can be downloaded again). A `pgdata` snapshot is consistent on a crash (Postgres recovers with the WAL); the `pg_dump` below is the logical backup, portable across versions. The Wiki.js sync with Git is an extra layer.
- **Local backup** (`ariadne backup create`, `ariadne backup verify`): a logical `pg_dump` (custom format) of the `ariadne` database, of `wikijs` and, if it exists, of `moodle`, plus the course repository, into a folder `backup-<date>/` with `MANIFEST.json` (sizes and sha-256) in `BACKUP_DIR` (`BACKUP_KEEP`, 4). It is **complete**: it carries private content and user data, so it **never leaves the data server**. Only the translation cache goes without data. It is restored with `pg_restore`. `pg_dump` does not filter rows by licence class, so it is not suitable for the external release.
- **Weekly external release** (`ariadne release check|build|run|verify|publish|status`; worker `release`): encrypted with **age** to the **public** key `RELEASE_AGE_RECIPIENT` (the private key is never on the system; Ariadne refuses a private key, or a key the image's `age` cannot use, and post-quantum keys, which require age ≥ 1.3), in parts of **at most 2 GB** (`RELEASE_PART_BYTES`), with `MANIFEST.json`, `SHA256SUMS` and a `restore.sh`.
  - **Authenticity.** The `MANIFEST.json` (which carries the size and sha-256 of each part and of the content, and that of `restore.sh`) is **signed** with a signing key of its own, distinct from the age key (`ssh-keygen -Y sign`, ed25519: `RELEASE_SIGNING_KEY_FILE`, read-only in the worker). The **public** key (`RELEASE_SIGNING_PUBKEY`) is pinned **outside GitHub** (`.env`, README, or kept by whoever restores). `restore.sh` and `ariadne release verify` check the signature **before anything else** and refuse if it fails; the public key is given as an argument, file or variable, **never read from the archive**. Without it, whoever controlled GitHub could swap parts and manifest for other consistent ones and nobody would notice (the fingerprints only prove integrity). The parts are validated against the **signed** manifest (not against a loose `SHA256SUMS`), and the `manifest_sha256` is compared with the `backup_run` when the installation knows the release. Honest limit: the signing key is on the server (the server does the signing), so it protects against tampering after construction, not against a compromised server (the private age key, on the other hand, is never there).
  - **Restore without running anything from the archive.** `restore.sh` (`ssh-keygen`, `age`, `tar`) checks the signature and the parts, asks for the private age key, decrypts, checks the sha-256, refuses dangerous paths and restores; with `--database NAME` (the connection comes from the `PG*` variables or from `~/.pgpass`: **never a password on the command line**) it creates the schema `ariadne_release` and loads the `db/*.csv` with `COPY … FROM STDIN`. The **schema comes from the code**, the archive carries only **data**: no SQL from the archive is ever run. The rehearsal (`release verify --identity`) does the same in a **temporary database and a temporary unprivileged role** (no superuser, no creating databases or roles), never with the `DATABASE_URL`.
  - **No clear text on disk.** The content is written as `tar.gz` streamed directly to `age`'s stdin (it only passes through memory and the pipe); the pages stay in memory until the end. Before each release there is a self-test of `age` with a disposable pair and a "canary" (a random text inside the content must not appear in the parts). The temporaries of a construction that died (`.ariadne-release-*.tmp`, encrypted parts only) are cleaned up at the next start. The preview `backup create --destination release` **is** clear text: it is written into 0700 folders and 0600 files, only the latest is kept, and the previous one is deleted.
  - **Included:** the open-class curated content (the pages Ariadne published to Wiki.js, with provenance and licence, and **only the open versions** of each canonical course: each `repo.load(slug, version)` has its own licence), the source register (only safe fields, without paths or `origin_source_id`) and the settings on an **allowlist** (numbers, switches and some model names: never URLs, emails, paths or secrets). The pages come from Ariadne's curation (what it published), not from a live read of Wiki.js: manual edits in Wiki.js are in the local backup.
  - **Never included:** user data (principals, keys, sessions, `query_log`, `query_result`, `tool_call`: the release does not even read those tables) and any content of the private or derived-from-private class. Content is selected with `document_is_open()` and the licence of each course version and, at the end, a check on **what was actually written** (the licence read from the `course.yaml` sent, `licenses.assert_exportable` over sources and documents, the settings against the allowlist): if anything private gets through, the release refuses, nothing is written and it is logged in `license_block`. The manifest (in clear) only has counts of what goes in.
  - **Left out** because it can be downloaded again: original ZIM files and model weights.
  - **Publication**: `ariadne release publish` (GitHub API, token `RELEASE_GITHUB_TOKEN`, repository `RELEASE_GITHUB_REPO`) creates the release as a draft, uploads the parts and only publishes it at the end, so an incomplete public release never exists. When resuming, only a file that is `uploaded` and of the right size counts as sent (the truncated ones are deleted and uploaded again); orphan `ariadne-release-*` drafts are deleted and **do not count** towards the retention of the last eight (`RELEASE_RETENTION`; no other releases are ever deleted). **Off by default** (`RELEASE_ENABLED=false`): `--dry-run` never touches the network, and the weekly schedule (periodic job) only runs with `RELEASE_ENABLED=true`, the age public key and the signature configured. A publication failure reuses the already-built folder (the same tag), it does not rebuild. Builds and ZIM downloads run in the `worker-heavy` service (14.3), separate from indexing.
- **Restore rehearsal** (`ariadne release verify <folder> [--identity key.txt] [--pubkey … | --pubkey-file …] [--record]`): without the age key it checks the signature, the manifest, the sizes and sha-256 of the parts, `restore.sh` (against the signed hash) and that the parts really are encrypted; **with** the private key (kept off the server) it decrypts, checks the content and the canary, restores into a temporary folder, loads the data into a **temporary database** with an unprivileged role and compares the counts, and confirms that nothing that went out is private. The app shows the last rehearsal and warns when it is more than `RELEASE_REHEARSAL_DAYS` (90) old or was never done. The server cannot rehearse on its own (it does not have the key): the rehearsal is a periodic action of the owner. A backup or release "running" with no activity for `BACKUP_STALE_HOURS` (6) is marked as failed.
- **Operations:** the *Operations* module of the app shows the last backup and the last release (size, parts, published), the rehearsal status and the next schedule.

---

## 17. Security

- **Injection stopped at intake:** curation cleans the content before it reaches the bases; searched text is always delivered to agents delimited and marked as data.
- **Agents do not execute anything directly:** they search and read. Code execution always goes through Jobe.
- **Isolated sandbox** (section 12).
- **Higher standard for health:** health content is always curated; answers carry a medical source; a warning that the system does not replace a health professional; with signs of urgency (chest pain, difficulty breathing), the first line directs the user to seek immediate medical help.
- **Authentication** by API key on all interfaces; Postgres reachable only locally.
- **Licence classes** enforce that private content never leaves.

---

## 18. Status and roadmap

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Embeddings benchmark without AVX | Done |
| 1 | Skeleton: compose, migrations, principals and keys | Done |
| 2 | MCP + level 1 (`search`, `read`, `sources`, `feedback`) | Done |
| 3 | Level 3 by use: embedder, queue, indexer, router, heat | Done |
| 4 | Neighbours and daily budget | Done |
| 5 | Graph between sources: article vectors, `linker`, links | Done |
| 6 | Article base: curation + Wiki.js | Done: curation pipeline (flags, LLM curator only for the specific cases, fidelity, review queue in `curation`), selective import into Wiki.js 2.5 with "Links" and licence, level 2 in the router, bounded output in MCP. Details in `ARCHITECTURE.md`, section 4.4.1 |
| 7 | Languages: concepts, `langlinks`, `translations`, LibreTranslate | Done: `concept` with importers for the Wikidata *sitelinks* dump and for `langlinks`, original language (by hand or from a countries TSV), `translations` tool, historical rule in the router ("original source"), "Other languages" section in Wiki.js, local translation with code markers, cache, PT⇄EN question merged by RRF, and `translate`. Details in `ARCHITECTURE.md`, section 8 |
| 8 | Web application / dashboard | Done: `web/` (React + Vite + TS) served by the server at `/`, with Reading, Graph, Statistics, Sources, Curation, Links and Operations; API `/api/stats/*`, `/api/principals` (roles `admin`/`member`), `/api/collections`, `POST /api/jobs/{id}/retry`, `/api/config`; migration 0008. Courses, Books, Licenses and the Kiwix catalogue were "coming soon". Details in `ARCHITECTURE.md`, section 12 |
| 9 | Wings and rooms (MemPalace) | Done: `room` table (migration 0009) and `document.room` as key; rooms by spherical k-means over the article vectors of each wing (`ariadne rooms build`, `rooms` worker), `wings`/`rooms` filters and merge bonus in search, rooms in `sources`, `rooms` tool, `GET /api/rooms`, room in the reader and room filter in the web app, "room" neighbours over the real rooms. MemPalace is an optional mirror (`ariadne[mempalace]`, `ariadne mempalace sync`): the package requires chromadb and its own vector table. Details in `ARCHITECTURE.md`, section 4.5 |
| 10 | Course base: canonical format, Moodle + CodeRunner, Jobe | Done: classification at intake (article, course, book; `document.content_type`), canonical format in Git (pydantic schema, one commit per change, version per course), freeCodeCamp extractor with the JS tests mapped to CodeRunner (Node), `course`/`course_version`/`lesson`/`exercise` index (migration 0010), lessons as documents in search and in the graph (article ↔ lesson, `requires`), `CourseBackend` for Moodle 4.5 + CodeRunner (idempotent), Jobe isolated behind a relay, `ariadne courses/moodle/sandbox`, `courses` worker and the app's Courses module. Details in `ARCHITECTURE.md`, section 4.7 |
| 11 | Book base: Calibre + Kavita, extraction by chapter, licence classes | Done: `LibraryBackend` (Calibre through an HTTP agent in the container), `BookSource` (`book:<id>`, one document per chapter), EPUB and PDF extraction without native dependencies with DRM refusal, text search over chapters with `book/chapter#position` citations, Kavita as the reader (URL in the results), chapter curation, mandatory `license_class` in `source`/`document` inherited by everything derived, `assert_exportable` on the outputs, Books and Licenses modules in the app, migration 0011. Details in `ARCHITECTURE.md`, section 4.8 |
| 12 | Kiwix catalogue in the dashboard; automatic backups and releases | Done: OPDS catalogue client with cache, `catalog` worker (download with `.part`, resume by `Range`, size and sha-256 from the `.meta4`, progress in `job.payload`, cancel), kiwix `library.xml` kept by the entrypoint (`--library --monitorLibrary`), archive/restore/delete ZIM, Sources module and full catalogue; `ariadne backup create` (pg_dump of the databases), `ariadne release build|run|verify|publish` (open class only, `assert_exportable`, age, parts ≤ 2 GB, manifest, `restore.sh`, restore rehearsal in a temporary database, GitHub publication off by default), `release` worker (weekly), `backup_run` (migration 0012) and the backups panel in Operations. Details in `ARCHITECTURE.md`, section 4.9 |

Phases 5 to 11 can be parallelized.

---

## 19. The future

Decisions taken, for after the phases above:

- **Courses from other platforms**, only from those that allow buying and downloading a full copy of the course.
- **Video transcription:** Whisper for the audio, with timestamps to cite the exact moment.
- **Screen-capture lessons:** besides the audio, extract what the teacher shows on screen (code, commands, slides), by OCR and description of the frames, aligned with the transcript.
- **Private sources from organizations:** Confluence, Azure DevOps, Notion, as new `Source` implementations, always in the private class.

---

## 20. Risks and open questions

| Risk / question | Mitigation |
|---|---|
| Native dependencies that assume AVX | Benchmark canary before entering `server` |
| Disk space of the curated bases | Selective import and curation, driven by use |
| Wiki.js version and API (2.x vs 3.x) | `WikiBackend` interface |
| Moodle's footprint on the data server | Validated on the Mac in Phase 10: installs in about 2 minutes and uses about 160 MB of RAM at rest; still to be run on the X5550 (canary in `bench/embeddings/README.md`) |
| Reliable extraction of course structure from the ZIM files | Done for freeCodeCamp (explicit structure and tests); tests that depend on the browser are marked "not mapped" with the reason. Other sources (LibreTexts, Wikiversity) need their own extractors |
| Jobe without Go and the virtual memory Node asks for | The image's Jobe has no Go (to be solved); Node needs ≥ 4 GB of virtual address space |
| Vector links with false positives | `proposed` state, conservative thresholds, curation |
| Quality of PT-PT translation | Own model in LibreTranslate if needed |
| Leak of private content | Mandatory licence class at intake (`NOT NULL`, no default), inherited in Postgres and checked on every output (`assert_exportable`); the curator and the translator must be local |
| Calibre and Kavita on the X5550 without AVX | Calibre is the official x86_64 binary (Qt 6 and its own Python) and Kavita is .NET: both only verified on arm64 on the Mac; canary in `bench/embeddings/README.md` (item 7). The server's extraction is pure Python |
| Kavita 0.9 does not read EPUB when the library metadata is off | The library is created with `enableMetadata` (`ariadne kavita bootstrap`) |
| Losing the private age key of the releases | The releases become unreadable for ever (that is the goal: the server does not have it). Keep it in two places outside the data server (password manager and paper/vault) and rehearse the restore from time to time: the app warns when the rehearsal is overdue |
| A ZIM leaves the Kiwix catalogue or changes its name | The download uses the `.meta4` of the moment (exact size and sha-256); a source already downloaded does not depend on the catalogue. A catalogue that is down is just a 502 in the app |
| Licences when publishing the project | The project distributes only code; content comes from the user |
