# Wiki.js import, v2 (design)

This replaces, for Wiki.js, the decision to use `/<source>/<language>/<slug>` paths (ARCHITECTURE 4.4, STRUCTURE).
Wiki.js is the main source for LLMs, so each page must come out **well formatted, with images and organized by
subject**, not by the structure of the ZIM file.

Decisions taken by the project owner (2026-10-25):
- **Domains:** a curated tree in Portuguese (editable), plus signals. Each document is placed by signals from its own
  source and by vector similarity, and low-confidence placements go to review.
- **Languages:** each language has its own path: `/pt/...`, `/en/...` (Wiki.js *namespacing*).
- **Whole books and manuals:** they go to Calibre/Kavita, not to Wiki.js.
- **Formatting:** deterministic rules first. The LLM is used only for bad cases, to restructure the form, with a
  fidelity check.
- **Batch:** LLM work runs first on free external GPU (Google: Kaggle/Colab), and whatever is left runs locally.

## 1. The sample (164 random articles from 41 ZIM files, plus the AUTOMAIN example)

Script: `sample_fetch.py`/`measure.py` (in the session scratchpad). The findings that generate the rules:

| Finding | Where | Rule |
|---|---|---|
| The converter drops all images | wikihow 10–28, lowtech 64, OSM 74, cd3wd 90 per page | §3 images |
| Text made only of `<br>`, no headings or paragraphs | cd3wd VITA (388–8359 `<br>`, 0 headings) | §2 reflow |
| Source noise («Back to Home Page…», language bar) | cd3wd, sites with menus | §2 repeated lines per source |
| Pages that are not articles | StackExchange `users/` profiles, Gutenberg covers, `genindex`, disambiguation, `Category:`, shell pages of 200–400 bytes (libretexts, freecodecamp), simulators (phet), empty device pages (ifixit) | §4 document type: `skip` |
| PDF inside the ZIM | survivorlibrary, armypubs, medlineplus | §4 `book` (Calibre) |
| Whole books and manuals | Gutenberg, wikisource (chapters), VITA AUTOMAIN (477 thousand characters, «By E. Christopher Cone») | §4 `book` |
| Page language ≠ ZIM language | wikem `/zh` `/it`, gentoo `/ja` `/ko` `/fr`, OSM `FR:` `Zh-hans:`, docs.python `ko/` | §5 language per page |
| Loose image captions, empty «References», ZIM licence line repeated | Wikipedia | §2 cleanup |
| Raw HTML that leaks (`<div style…>` inside text) | wikiversity | §2 escape the rest |
| Wikipedia, StackExchange, wikem, nhs, IEP, citizensinformation | — | already come out well (headings, lists and tables correct) |

## 2. Formatting (deterministic)

1. **Text reflow with `<br>`:**
   - A block with more `<br>` than paragraphs is rebuilt: `<br><br>` makes a new paragraph, and a single `<br>`
     joins broken lines.
   - A word hyphenated at the end of a line is joined («manuten-\nção» → «manutenção»).
   - Short lines in UPPERCASE, or numbered («1. INTRODUÇÃO», «CAPÍTULO 3»), become `##`/`###` headings.
   - Lines starting with `-`, `•`, `o `, `a)` or `1)` become list items.
2. **Source noise:** a line (or short block) that appears in more than 30% of the sampled pages of the same source is
   navigation and is removed. The sample is computed once per source and stored.
3. **Native Wiki.js forms:** GFM tables; information and warning boxes in `> … {.is-info}`/`{.is-warning}`; code
   blocks with a language; formulas in `$…$` (KaTeX); a figure as an image plus an italic caption.
4. **Cleanup:** headings with no content («References», «See also» left empty) are removed, and so is the ZIM's own
   licence line (the footer already has it). Any HTML left over is escaped.
5. **Bad case → LLM:** after the rules, a page with more than 3,000 characters, no headings, and average paragraphs
   above 1,500 characters (or with low-confidence reflow) is marked `format_rescue`. It goes to the LLM batch (§7),
   which may only change the form; the fidelity check (curation/fidelity) compares the facts.

   **What the sample did not see** (the sample is 60 articles from a large source, or up to 150 from a small one,
   `boilerplate.sample_size`): link lines and link blocks (menus, language bars) are compared without separators
   (`- • |` and spaces) and are removed after 3 pages, whatever the fraction. In addition, structural rules that do not
   depend on the sample: a link bar at the top or bottom of the text (links separated only by separators or by
   «or/and»: «Home - English - French», «Back to X or Back to Y»), an outbound image link at the end («Donate»), and an
   image with «logo»/«banner» in its name before any text are always removed. The summary (`render.summary`) is never
   a navigation bar and prefers running prose (sentences, 12 words, few numbers).
6. **Triage without false alarms** (`curation/flags.py`): hidden text raises `hidden_text` only if it looks written for
   a model (vocabulary and instruction phrases, an imperative addressed to «AI/assistant/you», a URL to send data to),
   or if it is long prose (400+ characters) that nobody repeats. JSON and data, short accessibility texts («By Country
   Listing of the values for…»), coordinates and anything the source repeats do not count: the `Boilerplate` stores
   the lines and the *templates* (the first 5 words) of hidden text that appears in 3 or more sampled pages
   (`Boilerplate.hidden`). Hidden text still goes through the `injection` and `dangerous_command` rules. The
   `encoded_blob` check ignores URLs and paths (`strip_urls`), however long the random stretch is.

7. **Page title** (`wiki/titles.py`): the ZIM's `<title>` is often the site name («Army Publishing Directorate»), a
   serial number («PAPEL #7 TÉCNICO») or the title with the site stuck on («… - MedlinePlus Medical Encyclopedia»,
   «[OpenWrt Wiki] …», «LOW-TECH MAGAZINE: …»). The candidates are the `<h1>`, the `og:title` and the `<title>`, without
   the site name, which is learned per source (a prefix or suffix, separated by `-`, `|`, `:`…, in more than 30% of the
   sampled pages: `source.meta.boilerplate.site`). A candidate that is the same on many pages (`generic`) or contains a
   serial number is weak. The `<h1>`/`og:title` that agrees with the `<title>` wins (the `<h1>` of a wikibooks subsection
   does not replace it); if one is the start of the other, the shorter one is used. Without a good candidate, the first
   heading in the text is used (after the reflow: on cd3wd, UPPERCASE lines are headings), and it is removed from the body
   because it has become the title. A title fully in UPPERCASE is changed to sentence case.
   The title is stored in `meta.wiki_title` and goes to the Wiki.js page and to the slug; the document title (search,
   links) does not change. If the title changes, `meta.wiki_path_title` no longer matches and the page is moved
   (`pages.move`) to the new path.

## 3. Images

- An `<img>` with a relative `src` is read from the ZIM itself (`read_bytes`). `data:` only up to 1 MB. Remote images
  are not fetched (the system works offline).
- Icons and logos are removed: sides under 32 px, or identical files in more than 30% of the source's pages (the same
  rule as for repeated lines).
- Upload to Wiki.js: `POST /u` (multipart, Bearer) to the assets folder `ariadne/<aa>/<sha256>.<ext>`. The name is the
  hash of the content, so the same image is never sent twice; before sending, a HEAD request checks whether it is
  already there. Limit: 5 MB (the Wiki.js limit); above that, the image stays as a link to kiwix.
- The Markdown carries `![alt](/ariadne/aa/<sha>.png)`, and the caption (`figcaption`, or the text around it in a
  `thumb`) goes below it in italics.

## 4. Document type (before import)

`classify(document) -> article | book | skip`, with a recorded reason:

- **skip:** HTML with fewer than 300 characters of text; shell page (redirect by meta or JS); user profiles
  (`/users/`); indexes (`genindex`, `Category:`, tag lists); disambiguation; pages that are only an app or simulator, or
  only media. They stay searchable, they just do not go to Wiki.js.
- **skip, listings:** pagination and archive pages, by path (`/page/2/`, `page-3.html`, `?page=4`, `/archive/`,
  `/category/x/`, `/author/x/`, `/blog/2021/05/`; `page_123` does not count, it is the id of a libretexts page) or by
  content: 3 or more «Continue reading/Read more» with 20% or more of the text in links, or 6 headings that are links
  with average paragraphs under 250 characters.
- **book:** a PDF or EPUB item; whole book sources (gutenberg, survivorlibrary, armypubs in PDF); HTML with more than
  60,000 characters that has an author byline («By X», «Por X») or more than 8 numbered chapters/sections; chapters of
  a wikisource book. It gets `meta.route = "book"` for Calibre (second round, once Calibre is on the data server). Until
  then, the domain page in Wiki.js shows it as «Book» with a link to kiwix.
- **article:** everything else, which goes to Wiki.js.

**PDF and EPUB inside the ZIM** (survivorlibrary, armypubs, medlineplus): the item is not HTML, so the source raises
`NotAnArticle` with the MIME type. `import_page` and the indexer (`index_document`, `article_vector`) catch it: they
record the `document` row (`register_book_candidate`, with the title taken from the file name if the ZIM does not give
one), with `meta.route = "book"` and `meta.wiki_skip = "PDF file"`, without an error. The indexer marks it as indexed
(there are no chunks), so search does not ask for it again, and the sweep ignores it because of `wiki_skip`. The book is
placed in the tree (the domain page lists it).

**In the importer** (`import_page`): it runs before preparation. A `skip` or a `book` does not go to Wiki.js: the document
gets `meta.wiki_skip = <reason>` (and `meta.route = "book"` for a book, which is also placed in the tree so the domain
page lists it), and `run_import_scan` never queues it again (`wiki_skip` is a condition in the queries). A page that was
already published and becomes `skip` or `book` is archived with the usual mechanism (a notice at the top, never deleted).
If the document becomes an article again, the mark is removed and curation takes it out of the archive. Manually,
`enqueue_import(force=True)` re-evaluates it.

## 5. Language per page

In this order: `<html lang>`/`<body lang>` → suffix or prefix of the path (`/zh`, `/ko`, `FR:`, `Zh-hans:`,
`/ko/3.12/`) → lightweight detector based on frequent words → the ZIM's language. The result is the language in
ISO 639-1 (`pt`, `en`, …), and from it comes the **Wiki.js locale** (section 5.1). Each language has its own path
`<locale>/<domain>/<subdomain>/<slug>`, as with `pt` and `en`.

### 5.1 All languages: from language to locale (owner decision, 28/10/2026)

Wiki.js 2.5 publishes in locales from its catalogue (70, `language.WIKIJS_CATALOG`) and only routes them if the code is
`xx` or `xx-yy` (`page.js` uses `^[A-Z]{2}(-[A-Z]{2})?$`; `sr-latn` and 3-letter codes do not work; the column
`pages.localeCode` still points to the `locales` table, so the locale must be installed). The rule (`language.wiki_locale`):

| Case | Locale | Examples |
|---|---|---|
| 3-letter code (ISO 639-3 or 639-2/B) with a 2-letter form | converted (`ISO3_TO_1`, 184 languages) | `por` → `pt`, `swa` → `sw`, `fra`/`fre` → `fr` |
| ISO 639-1 language with a locale in the catalogue | the code itself, or the only one in the catalogue | `fr`, `is` → `is-is`, `hi` → `hi-in`, `eu` → `eu-es`, `mk` → `mk-mk` |
| Norwegian with no further detail | `nb` (the catalogue has `nb` and `nn`, not `no`) | `no`, `nor`, `nob` → `nb`; `nno` → `nn` |
| Regional or script variant | joins its base language | `en-GB` → `en`, `pt-PT` → `pt`, `sr-Latn` → `sr`, `zh-Hans` → `zh` |
| The two variants that have their own locale | separate locale | `pt-BR` → `pt-br`; `zh-Hant`, `zh-TW`, `zh-HK`, `zh-MO` → `zh-tw` |
| Language with only 3 letters (`ceb`, `war`, `arz`, `min`…) or outside the catalogue (`mg`, `cy`, `be`, `sh`…) | **no locale** | |

The variant is read from `<html lang>` and from the ZIM's language. **A page without a locale is not published:** it stays
indexed and searchable in Ariadne, with `meta.wiki_skip = "language <xx> has no locale in Wiki.js"` and
`meta.wiki_locale = <xx>` (the ZIM's 3-letter code, for the report), and the sweep does not queue it again. The language
is never misreported (Cebuano is never put under `en`). A multilingual ZIM (`eng,fra,…`) whose page does not declare a
language gets `und`: it is not published either.

The catalogue of 603 ZIM files has hundreds of languages; those that have a locale cover almost all pages, but excluded
are, among others, Cebuano (10.1 M), Waray (2.1 M), Egyptian (1.7 M), Malagasy (6.2 M), Serbo-Croatian (5.0 M) and Welsh
(0.4 M). They remain an open decision (ARIAD-32).

**Locales installed in the production environment (28/10/2026).** All 69 routable catalogue locales (except `sr-latn`) are
installed and in the `namespaces`, with the site locale set to `en`. The API `localization.downloadLocale` always returns
«success» (the job swallows the errors), and `graph.requarks.io` refuses bursts of requests: each locale is downloaded one at
a time, `isInstalled` is confirmed, and only then does it enter the `namespaces`. Putting a locale that is not installed
into the `namespaces` makes `updateLocale` fail with «No such locale in local store» **after** saving the configuration,
and the importer would then consider the locale active and fail to create the page.

**Wiki.js:** namespacing must be enabled, with the locales in use installed (the production environment has it enabled
with all of them, section 5.1; the Wiki.js of the development compose does not, and has only `en`). The importer does not
change this configuration. The locales it publishes to come from Wiki.js (`localization.config`: the `namespaces` plus the
site locale, cached for 5 minutes) or, if `WIKIJS_LOCALES` is set, from that variable (default `en,pt`, which also applies
if Wiki.js does not respond). A page in a language with no active locale does not go to Wiki.js: it gets
`meta.wiki_skip = "locale <xx> inactive in Wiki.js"` and `meta.wiki_locale`, and the sweep does not queue it again. The
detected language is stored in `meta.wiki_locale`. The page tags are the language and the domain id (without the source's
slug).

Because namespacing puts the locale in front of the URL, `meta.wiki_path` stores `<locale>/<path>`, and internal links,
`wiki_url` and the release use it as it is.

## 6. Domains and paths

- **Path:** `/<locale>/<domain>/<subdomain>/<title-slug>`, with the domain names in the locale's language
  (`/pt/transportes/automoveis/…` and `/en/transport/cars/…`). Paths that collide get today's stable suffix.
- **Tree:** `server/ariadne/wiki/domains.yaml` contains about 22 top-level domains with subdomains. Each has an id,
  `pt`/`en` names and a `pt`/`en` description (used for the vectors). It is stored in the `domain` table and is edited
  through the API (the dashboard screen comes later).
- **Placement** (`domain_assignment`: document, domain, confidence, signals):
  1. *Source prior:* a specialized source fixes the domain, for example `mechanics.stackexchange` → transport/cars,
     wikem/mdwiki/nhs/medlineplus → health, devdocs/openwrt/gentoo → computing, wikivoyage → geography and travel,
     publicdomainrecipes → food.
  2. *Anchors (kNN):* documents already placed by `prior` or by a person (`human`) are labelled examples (at most 200 per
     domain, by article vector). The document votes for its 10 nearest neighbours; a neighbour counts only if the z-score
     of its similarity against all anchors is at least 1.5 (e5 cosine is compressed: 0.80 ± 0.04 between any texts) and
     the cosine is at least 0.75, and the similarity of each anchor is discounted by its similarity to random documents
     (the attractor). It starts here when there are 5 close neighbours: with 60% of the (weighted) vote in a top-level
     domain and the descriptions agreeing, or 85% alone, the placement is accepted without review (the leaf, within the
     domain, is chosen by the descriptions). If the descriptions disagree with a 60–85% vote, the descriptions decide, but
     the placement goes to review.
  3. *Description vectors:* the cosine between the centroid of the document's chunks and the vector of each subdomain
     description (e5, «passage:»), **minus the average similarity of the leaf to 400 random documents** (the attractor:
     `sobrevivencia/abrigo-e-fogo` looked like everything). Without 30 documents to measure it, there is no correction (the
     previous rule). The confidence is the best score minus the second, with an absolute minimum: z >= 3.5 and margin >= 0.01
     with the correction (3.0 and 0.005 without it).
  4. *Source structure:* documents linked from the same index page inherit the majority domain (for example the cd3wd
     indexes). The importer passes `structure_hint=None` for now: this signal is left for a later round.
  5. Below the threshold the placement is marked `needs_review` and the importer queues the `domain_classify` job (§7),
     which the LLM batch resolves (`method = "llm"`, which nothing automatic changes); without a batch, a person does it.
     Until then, the page stays at its best guess or in `/<locale>/por-classificar/…` (to be classified).
- **Domain pages:** `/<locale>/<domain>` and `/<locale>/<domain>/<subdomain>` are generated. They show the description, the
  parent domain, the subdomains, the articles and the books (with a link to kiwix; Kavita when available). The order is by
  title, each list holds at most 500 entries («… and N more») and the text is deterministic. When a page is created or
  changes domain, the importer queues the `wiki_domain_index` job (one per locale and domain, deduplicated, low priority);
  the worker writes the domain page and those of the parent domains it has not yet written, and rewrites a page only if
  its text changed (the memory lives in the process). They carry the tag `dominio` (domain). An article never takes the
  path of a domain page: it gets the stable suffix.
- **Pages already published:** they move to the new path with `pages.move`, which keeps the history. The old format
  (`<source>/<language>/<slug>` in the site locale, without `meta.wiki_layout`) migrates when the page is re-imported;
  `meta.wiki_at` says where the page actually is, and if `move` fails (Wiki.js down) the job repeats and resumes where it
  left off. A path in the new format stays as it is (links do not break), except in `por-classificar` (retried on every
  import) or if a human/LLM placement changes the domain, or the locale changes. Moving a page redoes its neighbours, whose
  links pointed to the old path.

## 7. LLM batch (external GPU first, local afterwards)

Implemented in `server/ariadne/compute/` and in the **Curation** module of the dashboard («Control and destinations»). The
contract is small: Ariadne writes a JSONL file of chat requests, someone answers them and returns a JSONL file of results.
No task type knows where it runs.

### Task types (registry)

A type is a subclass of `ariadne.compute.tasks.TaskType` with the `kind` of the queue job it handles:

- `prepare(services, session, job) -> Prepared | None`: the requests to the LLM (`BatchRequest`: `key`, `messages`,
  `max_tokens`) and a `meta` (the fingerprint of the text, the document, …) that comes back at import time. `None` = this
  job does not go into a batch (already decided, private content): it stays for the local run.
- `apply(services, session, job, meta, replies, run_id, model) -> Applied`: validates the replies with the same rules as
  the local curator and stores them. The result is `APPLIED`, `REVIEW` (stored, left for human review), `STALE` (the text
  has changed: no longer needed) or `INVALID` (not usable: the local run tries).
- `register(MyType())` when the module is imported. The `Runner` now respects the panel (pause, window, destination) on its
  own for **all** registered types; the job's local handler does not change.

Registered today: `curate_page` (`ariadne/compute/curate.py`) and `domain_classify`
(`ariadne/compute/domain_classify.py`: the prompt gives the title, the summary and the 8 best candidates from the kNN and
the descriptions; `apply` checks that the domain exists and stores `method="llm"`, asking for the page to be re-imported so
it moves; the job's local handler, which has no LLM, postpones it and only completes it after it has gone through a batch).
`format_rescue` works in the same way, without the `pending_batch` state of the earlier design: the jobs remain `pending`
with their `kind`; when they go into a run they become `batched` (no worker claims them) and return to `pending`, with
`batch_tried`, if the run does not consume them.

### Destinations (`ComputeBackend`)

| Destination | Mode | What it does |
|---|---|---|
| `local` | local | The **Mac** (Mac Studio, `llama-server` with Metal, `CURATOR_URL`), through the workers; with the Mac off, jobs wait in the queue |
| `kaggle` | automatic | Private dataset + `kaggle kernels push` + status + output, all driven from Ariadne |
| `colab` | manual | The panel generates a `.zip`; you run the notebook; you upload `ariadne_results.zip` |
| `paid` | reserved | Ready interface (`submit`/`poll`/`fetch`), **not implemented** |

Facts confirmed (25/10/2026): the official Kaggle CLI (`kaggle`, PyPI package `kaggle`) supports `kernels push` (with
`--accelerator NvidiaTeslaT4`, which is T4 ×2, and `-t` with the time limit in seconds), `kernels status`,
`kernels output`, `datasets create` (private by default) and `datasets status`. Authentication is `KAGGLE_API_TOKEN` (or the
legacy key `KAGGLE_KEY`) plus the username. Colab has a runtimes API, but only in beta and behind an access list
(developers.google.com/colab/api): without it, Colab stays manual. The Colab FAQ forbids, on the free tier, remote control
and using the page only as a web interface; so it is not automated. **Not confirmed in an official source:** Kaggle's
quota of about 30 GPU hours per week and the maximum session length (the documentation pages did not render, and the
secondary sources disagree between 9 and 12 hours). They stay as editable values (`KAGGLE_WEEKLY_GPU_HOURS`,
`KAGGLE_SESSION_HOURS`, and «already used» in the panel); Kaggle does not expose the quota through an API, so Ariadne
counts the time of its own runs.

### The flow of a run

1. **Export:** chooses up to `batch_size` pending jobs that have not yet gone to any batch, calls `prepare` on each one and
   writes `tasks.jsonl` (`{id, kind, prompt_version, messages, max_tokens, temperature}`; the `id` is
   `r<run>:j<job>:<request>`). The jobs move to `batched`. Private content never leaves (`assert_may_send`): it stays for
   the local run.
2. **Send** (Kaggle): private dataset `ariadne-batch-<n>` with `tasks.jsonl` and `manifest.json` (model, deadline,
   context), and the notebook with that dataset as input, GPU on and the deadline in `-t`.
3. **Watch:** the periodic `compute` task (every 30 s) checks the kernel status; at the end it collects `results.jsonl`
   (and `results.meta.json`, with the GPU time) and deletes the dataset.
4. **Import:** each task in its own transaction. It validates the schema (curator JSON), fidelity (`curation/fidelity.py`),
   the flags that must disappear, and the HTML/link cleanup, exactly as in the local run (`conclude_curation`).
   **What passes stays `needs_review`** (human review, with the run recorded): it is only published automatically with
   `BATCH_AUTO_PUBLISH=true`. The same applies to the local curator: with `false` (the default), what the verifier approves
   also stays `needs_review`, waiting for human review.
5. **Leftovers:** what the GPU did not answer, or answered badly, goes back to `pending` with `batch_tried` and the local
   curator handles it. «Consume there first, then here»: with an external destination active, the local side only claims
   what has already gone to a batch, or everything if the destination is blocked (no credentials, quota used up, three runs
   in a row failing). The local reserve is switched off in the panel.

### Control (dashboard and `ariadne batch`)

- **Play / pause / resume / stop;** the initial state (`running`, destination `local`, no window) is the earlier behaviour.
  Pause: nothing new is launched or consumed; a run already launched on Kaggle continues (there is no cancellation by API)
  and its results are imported. Stop cancels it and returns the tasks.
- **Scheduled window** («starts at 23:00, stops at 07:00», editable time zone, may cross midnight) or **Start now** (ignores
  the window until paused). The kernel deadline is the smallest of: hours per run, the destination's session, what remains
  of the quota and what remains of the window. The notebook stops by itself before the deadline and returns what it did.
- **Progress:** in the queue, in progress (and how many in an external batch), done, failed; GPU hours used and remaining for
  the week; recent runs with the tasks applied, for review and returned.
- **Credentials:** stack variables on the orchestrator (`KAGGLE_USERNAME`, `KAGGLE_API_TOKEN` or `KAGGLE_KEY`); the panel
  only says whether they are set.
- **Batch review** (a tab of the same module): lists what came from outside (all runs, or one), selects the faithful ones,
  views original and curated side by side, and approves or rejects them all at once (`POST /api/curation/review-batch`; an
  item that changed in the meantime is set aside, without undoing the others).
- **Without the panel:** `ariadne batch export batch.zip --limit 200` (Colab), `ariadne batch import ariadne_results.zip`,
  `ariadne batch status`.

### The notebook (`deploy/gpu/ariadne_batch.ipynb`)

Generic: it reads `tasks.jsonl` and `manifest.json`, answers with vLLM, and writes `results.jsonl` and `results.meta.json`.
It knows nothing about Ariadne or the task type, and it **never contacts it**. Default model `Qwen/Qwen2.5-7B-Instruct-AWQ`:
4 bits (~5.5 GB) fit comfortably on a 16 GB T4 and leave room for the KV cache (GQA, ~57 KB per token); the T4 has no bf16,
so the local Gemma 3 (which overflows in fp16) does not work; it is multilingual (PT/ES/FR/EN), follows JSON output well, and
is Apache-2.0. Heavier alternative: `Qwen2.5-14B-Instruct-AWQ` on the two Kaggle T4s. vLLM is pinned (`0.8.5.post1`, V0 +
xformers for the T4): **it was not run on a real GPU in this round**; the contract, parsing and validation are tested with a
fake destination, and the first real run should be small (low `batch_size`).

### Network and tunnel (optional)

Without the tunnel, Ariadne only makes outbound connections (the Kaggle CLI) and nothing reaches it from the internet: the
notebook reads a dataset and writes its output. This is the base path and the fallback plan.

With `GPU_GATEWAY_PUBLIC_URL` set, a run also carries a **token**, and the notebook talks to the **gateway**
(`ariadne gpu-gateway`, `ariadne/compute/gateway.py`) through a Cloudflare tunnel:

```
Notebook ──HTTPS──▶ Cloudflare (gpu-batch.<domain>, only /gpu/v1/…) ──tunnel──▶ [gpu-gateway container:
                                                     cloudflared ──▶ localhost:8810 gateway] ──▶ batch_run
```

- **The tunnel exists only during runs:** `cloudflared` is a child process of the gateway
  (`ariadne/compute/tunnel.py`). Every 10 s the gateway asks the database whether there is an open run with a valid token;
  if there is, it starts `cloudflared` (and starts it again if it dies); if there is no longer one, it stops it 45 s later
  (the response to `finish` must still get back). If the database does not respond, it counts as «no run» (it fails
  closed). The ingress is generated at startup and only lets through `^/gpu/v1/runs/[0-9]+/(tasks|results|heartbeat|finish)$`
  to `localhost`; everything else is a 404 at Cloudflare. The credentials are the JSON from `cloudflared tunnel create`
  (`GPU_TUNNEL_CREDENTIALS_FILE`, on the data server, read-only); the process only reads the `TunnelID` from it.

- **Exposed surface:** four routes, all for a single run, and nothing else (no `/docs`, no `/api`, no `/health`; a test fails if
  another route appears): `GET /gpu/v1/runs/{id}/tasks` (the requests), `POST …/results` (appends results, up to 5 MB per
  call, each line must belong to this run), `POST …/heartbeat` (`{done, total, gpu_seconds}`: the panel shows real progress)
  and `POST …/finish`.
- **Token:** one per run (`gpugw_` + 32 random bytes; the database stores only the SHA-256). It is valid until the run's
  deadline plus 30 min, and dies when the run ends, is cancelled, or the notebook calls `finish`. A failed authentication
  always returns the same 401 (it does not reveal whether the run exists). Limit of 240 calls per minute and per run.
  **Before reaching the database:** no body may exceed 5 MB (chunked bodies too, cut off as they arrive: 413), ids out of
  range and tokens without the prefix get a 401 straight away, and an IP (`CF-Connecting-IP`) with more than 20 failures per
  minute, or more than 300 failures per minute in total, gets a 429 without a database query. On Kaggle the token is in the
  `manifest.json` of the private dataset (it is never pasted by hand); on Colab it is in the package, and **each download of
  the package generates a new token** (the previous one stops working).
- **The gateway does not validate or publish:** it only stores results in `batch_run.results_gz`. The worker imports them,
  with the same schema, fidelity and sanitization as always, and the result stays `needs_review`. A forged result never
  reaches a published page (with the tunnel on, keep `BATCH_AUTO_PUBLISH=false`).
- **Limited Postgres role:** `deploy/postgres/gpu-gateway.sql` creates `ariadne_gateway`, which only reads `batch_run` and
  only updates the tunnel columns (a test confirms that it cannot read `principal` or `curation`). Put its URL in
  `GPU_GATEWAY_DATABASE_URL` (in the production environment, the operator's own deployment notes create the role and store
  the connection on the data server).
- **Fallback and salvage:** the notebook keeps writing `results.jsonl` to the output; if the tunnel fails midway, the results
  from the tunnel and from the output are merged (the last one wins). If the kernel is killed before finishing, or the
  deadline runs out, what the tunnel has already received **is imported** instead of being lost. Colab uses a «thin»
  package (only the manifest) and pulls the requests through the tunnel.
- **Results from outside:** the gateway rejects blocks more repetitive than an LLM would write (gzip bomb), joins blocks without
  line ends without pasting them together, and the worker never decompresses more than 512 MB per run. The JSONL of requests
  escapes U+2028/U+2029/U+0085 (the notebook's `splitlines()` used to break on them), and the gateway and worker split only
  on `\n`. `heartbeat`/`finish` only change a run that is still open (in the SQL).
- **Limits:** Kaggle and Colab have no fixed IPs (there is no IP list); Cloudflare sees the traffic (open-licence text: private
  content is never exported); a stolen token within the run window only allows sending false results (which go through
  validation and human review) and reading that run's requests.

## 8. Order

1. Formatting, image, document-type and language rules, with tests on the sample (small fixtures).
2. Domains: tree, placement, paths, domain pages, moving the old pages.
3. Integration into the importer (done: section 9); redeploy; republish the sample and review it in Wiki.js.
4. GPU batch (control panel, destinations, export/import and notebook): done, see §7; still to connect the
   `format_rescue` type (registry `ariadne.compute.tasks`); `domain_classify` is already connected.
5. Batch loading of all ZIM files.

## 9. How it ended up in the importer (`ariadne/workers/importer.py`)

Order in `import_page`: read → `doctype.classify` → `language.detect` → (`skip`, book or inactive locale: mark and stop) →
source noise (`learn_for_source` on the first import of the source or when its version changes, in `source.meta.boilerplate`,
which source synchronization preserves) → `convert.prepare` → `convert.images` + `ImageUploader.upload` (with Wiki.js down
the job is postponed, as always) → `to_markdown` with the URLs → `format_quality` → curation → publish (place, reserve the
path, move if needed, write).

- **Tree and vectors:** `domains.sync_tree` and `ensure_vectors` run on first use of the process (the importer or the sweep)
  and hourly; they are idempotent. Without an embedder (level 3 disabled) the model stays empty: placement uses only the
  source prior, and the rest falls into `por-classificar` with `needs_review`. The domain tree and vectors are loaded once
  (`domains.load_snapshot`, a 5-minute cache in `Services.wiki_cache`), and `place(…, snapshot=…)` no longer reads them for
  each document.
- **Bad formatting:** `meta.format_rescue = <reason>`; the page is published with the result of the rules and nothing is
  queued (the LLM batch is for another round).
- **Keys of `document.meta`:** `wiki_path` (`<locale>/<path>`), `wiki_at` (where it is now), `wiki_layout` (2), `wiki_domain`,
  `wiki_locale`, `wiki_skip`, `route`, `format_rescue`.
- **Deviation from the design:** the single `WIKIJS_LOCALE` no longer sets the locale of pages (only of searches and of
  anyone who does not say which). With namespacing disabled, the `/<locale>/…` links do not resolve: it must be enabled
  before publishing.
