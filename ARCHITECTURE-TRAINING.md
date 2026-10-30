# Ariadne — Model Training Architecture

This document describes the second layer of the Ariadne project. The first layer (ARCHITECTURE.md) is the knowledge service: ZIM, Wiki.js and pgvector served over MCP. This layer covers the models that use that service: how they are chosen, trained, evaluated, served and monitored. It is kept separate on purpose, because the first layer is reusable infrastructure and this one is a system of its own, which could serve much more than Ariadne.

Status: decisions closed in brainstorming (24/10/2026). None of this is implemented before Phase 1 of Ariadne is running.

## 1. Root principles

- **Always offline.** The system works without internet. The cloud is only an optional boost for training.
- **No Chinese models** anywhere in the system. There is an approved model list, and the system refuses to load anything not on it.
- **Uncompressed models as the base.** The base models are used with their original weights, trained from scratch at their full size (no pruning, distillation or quantization).
- **Models hold no state.** Context, sessions and history live in the control plane.
- **Everything inside the models is in English.** Translation is done outside them, by LibreTranslate.
- **Validate before building.** The full architecture only moves forward if the proof of concept (Phase 2) shows that it works.
- **Everything visible on the dashboard.** Curation, training, evaluation, loaded models and system health.

## 2. Phases

| Phase | Content |
| --- | --- |
| 1 | Ariadne as a knowledge service (ARCHITECTURE.md). Useful on its own for any model (Claude, Hermes, local models). Generates the usage logs. |
| 2 | Proof of concept of the research model, in a single domain. |
| 3+ | Router, specialists, aggregator, model manager, watchdog, distributed nodes, optional cloud. Only if Phase 2 proves its value. |

## 3. Phase 2 — Proof of concept

Central question: **does a small model that researches in Ariadne answer better than the same model alone?**

- Single domain: Python programming (plenty of data, objective verification).
- One specialist: SmolLM2-360M with an adapter. No router, no aggregator, no cloud.
- A few hundred test questions.
- Evaluation with automatic checks, and Claude as the judge.
- Comparison between three variants: base model without research, base model with research, trained model with research.

If the third variant clearly beats the other two, the idea has merit. Use of Claude is limited to this initial validation and is not a central part of the system.

## 4. Overview of the target architecture

```
Question (PT)
   │  LibreTranslate → EN
   ▼
Router (trained open-jev)  → score per domain + typed questions
   │  parallel fan-out
   ├── Specialist A ─┐
   ├── Specialist B ─┼── all query Ariadne over MCP
   └── Generalist   ──┘
   │  fan-in
   ▼
Aggregator (SmolLM2-1.7B)  → checks citations, resolves contradictions
   │  LibreTranslate → PT
   ▼
Answer
   └──→ log → curation → training
```

## 5. Router

- Based on **open-jev** (an open reconstruction, in PyTorch, of the ideas behind Jev from TypeSafe AI), trained from scratch by us. It does not generate text: it returns typed decisions with calibrated probabilities.
- Typed questions asked for each message: a score for each domain; does it need code (yes/no); language (Python, Node, front-end, Go); is it health-related (yes/no).
- It also reads the recent history of the session.
- Training data: questions from the Stack Exchange sites present in the ZIMs (each site is a ready label) and the Ariadne logs.
- The licence of the open-jev repository is to be confirmed before adoption.

### Routing policy (initial values, tuned after validation)

1. Call all domains with a score ≥ 0.30.
2. Always call at least the best-scoring domain.
3. If the best score is below 0.50, also add the generalist specialist.
4. Ceiling of three specialists per question; the administrator may set a higher ceiling.

Example (heart-rate monitor): programming 0.85, practice 0.60, health 0.45. The three run in parallel; the health rules apply only to the health claims.

## 6. Initial domains

| Domain | Main sources |
| --- | --- |
| Programming and systems | Stack Overflow and technical SE sites, DevDocs, Python/PHP docs, Arch, Gentoo, Alpine, OpenWrt, freeCodeCamp |
| Science and mathematics | Science SE sites, ProofWiki, PlanetMath, AoPS, LibreTexts, PhET, Wikispecies |
| Health | MDWiki, WikEM, MedlinePlus, NHS, CDC, LibrePathology, WikiVet |
| Practice and survival | Appropedia, Energypedia, CD3WD, Ready.gov, Survivor Library, iFixit, gardening, cooking |
| History and humanities | History SE, Wikisource, Wikiquote, Gutenberg, IEP, World Factbook |
| Languages | Wiktionary (6 languages), language SE sites, Linguistics |
| General (fallback) | Wikipedia, Wikibooks, Wikiversity, Vikidia |

First wave of specialists: programming, health and history. The rest stay with the generalist until there is enough data.

## 7. Specialists

- One shared base model (**SmolLM2-360M**, Hugging Face, Apache 2.0) with **one LoRA adapter per domain**.
- Each specialist sits behind its own HTTP endpoint, compatible with the OpenAI API.
- Each specialist may refuse ("outside my domain"), which neutralizes wrong routing.
- All of them research the same Ariadne over MCP; the knowledge lives in one place.

### Code generation

- Separate from technical research: its own base model, **StarCoder2-3B** (BigCode — Hugging Face and ServiceNow, OpenRAIL-M).
- **One adapter per language**: Python, JavaScript/Node, front-end JavaScript, Go. Other languages (e.g. embedded C++) come later.
- The base model always stays resident; the adapters load and unload in under one second.
- Pre-loading: when the router detects the language, the adapter starts loading while the rest of the flow starts.
- The code is verified in a sandbox (Python, Node, Go; Playwright for front-end). Compiling and passing tests is also the reward for reinforcement learning.

## 8. Aggregator

Model: **SmolLM2-1.7B** (same family as the specialists, same tokenizer and pipeline). Next step up, if needed: Gemma 3 4B, Phi-4-mini, Llama 3.2 3B or Ministral.

Four steps:

1. **Verify:** the cited passage exists in the document (exact check, no model); then, the passage supports the claim (with a model). What fails is discarded.
2. **Group:** equivalent claims from different specialists gain weight.
3. **Resolve contradictions:** prefers the source most specific to the domain (e.g. official documentation over a generic wiki). With no clear preference, it shows both positions with their sources.
4. **Write:** final answer in English, containing only what survived; LibreTranslate translates it at the end.

Golden rule: if nothing solid remains, the aggregator says it found no reliable answer.

## 9. Contract between the parts

```json
// Request (router → specialist)
{
  "request_id": "uuid",
  "session_id": "uuid",
  "priority": "admin | user",
  "domain": "programming.python",
  "question_en": "...",
  "context_en": "summary of the recent history",
  "deadline_s": 120
}

// Response (specialist → aggregator)
{
  "request_id": "uuid",
  "status": "answered | refused | partial | error",
  "refusal_reason": null,
  "answer_en": "...",
  "claims": [
    {
      "text": "short claim",
      "citations": [
        { "doc_id": "wiki/page", "revision": "id", "quote": "exact passage" }
      ]
    }
  ],
  "confidence": 0.78,
  "trajectory": ["searches and reads, in order"],
  "model": { "base": "SmolLM2-360M", "adapter": "python", "adapter_version": "v3" },
  "latency_ms": 4200
}
```

Decisions: citations per claim; a `partial` status instead of making things up; the trajectory is always included (it is training data); model and adapter version in every response; a deadline per request, and the aggregator carries on without any specialist that does not answer in time. Citations point to the exact revision in Wiki.js.

## 10. Language and translation

- All internal processing is in English.
- **LibreTranslate** (already present in the production environment) translates the question into English and the answer into Portuguese.
- Code, citations, function names and technical terms are replaced with placeholders before translation and restored afterwards.
- Search also queries the sources in Portuguese.
- The Argos models tend towards PT-BR; if the quality for PT-PT is not good enough, a model of our own is trained and loaded.

## 11. Curation: ZIM → Wiki.js

The ZIM is only raw material. **Wiki.js is the curated corpus**, from which search and training draw. Protection against instruction injection and harmful content happens at the input.

### Hybrid pipeline

1. Extract the article from the ZIM.
2. **Deterministic conversion** from HTML to Markdown, and cleaning of menus, footers and junk.
3. **Flag triage.** Only what triggers flags goes to the curator:
   - injection patterns ("ignore previous instructions", system tags, hidden text);
   - long encoded blocks (e.g. base64);
   - dangerous commands in code (`curl | sh`, `rm -rf`);
   - clinical doses and values.
4. **Always curated:** all health content.
5. **Random sampling:** a small percentage of the articles without flags also goes to the curator, to tune the rules.
6. **Curator:** evaluates, cleans and rewrites in a standard format. Main candidate: Gemma 3 12B at full precision (alternatives: Mistral Small 24B, Phi-4 14B). Runs in batches, when the specialists are idle.
7. **Fidelity check:** the facts of the original and of the rewrite must match; if the curator changed a number or a claim, the article does not pass.
8. **Publication to Wiki.js** with provenance (ZIM, version, path) and licence/source attribution (CC BY-SA where applicable).
9. **Human review queue** for doubtful cases.

Evolution: the regex-based triage may later be done by open-jev ("does it need curation?"), trained on the curator's decisions. Curation follows selective import: curate what is used. Large batches may go to a cloud GPU.

### Updating ZIMs

- When a new ZIM arrives, each article is compared with the previous version by a fingerprint of its cleaned text.
- Unchanged: kept. Changed: sent back through the pipeline and produce a new revision in Wiki.js. Removed: archived, never deleted.
- The old ZIM is kept for a transition period.
- Each training run records the revision of every text used, so that it can be reproduced.

### Origin of the ZIMs

- Phase 1: manual download, in batches by size (below 10 GB, then below 50 GB, and so on). Ariadne only reads the folder on the data server and queues the new files.
- Next phase: optional download from the Kiwix catalogue, in the dashboard (worker `catalog`).

## 12. Security

- Searched content always enters delimited and marked as data; the specialists never follow instructions that come from it.
- Attack examples are included in the training and in the test set.
- The specialists do not execute anything: they only search and answer. Actions go through the system.
- **Code sandbox:** disposable container, no network, read-only disk with a temporary folder, hard limits on time, memory and processes. Runs on the hypervisor, outside the Mac.

## 13. Health

- All health content goes through the curator.
- Health claims require a citation from a medical source and high confidence; otherwise, they are discarded.
- The specialist says clearly when it does not know.
- The aggregator adds that the system does not replace a health professional.
- Questions with signs of urgency (chest pain, difficulty breathing, etc.): the first line directs the user to seek immediate medical help.
- In the evaluation, no regression in the health domain is accepted.

## 14. Training

- Tools: MLX on the Mac Studio M2 Max; PyTorch in the cloud, with conversion of the adapters to MLX. The pipeline is designed for both sides from the start.
- Small specialists: full fine-tuning or LoRA. Aggregator and StarCoder2: LoRA.
- Data: real trajectories (Ariadne logs) and synthetic ones; answers with negative feedback serve as negative examples in reinforcement learning.
- Rewards: correct answer with a valid citation; code that compiles and passes tests; penalty for invention.
- Frequent checkpoints (free GPUs stop mid-run).

### Curation of training data

1. Only trajectories with verified citations enter.
2. A quality signal is mandatory (positive feedback or a high score from the judge); without a signal, the trajectory is quarantined.
3. Real questions weigh more than synthetic ones, and synthetic ones have a proportion ceiling.
4. The golden test set never enters the training (automatic duplicate check).
5. Traceability: each adapter knows which trajectories it was trained with, and allows problematic data to be withdrawn.

### User `trainer`

- A dedicated user of the system, whose questions and trajectories exist only for training and may be public.
- A good question from a real user may be redone by `trainer`, rephrased and without personal data; the new trajectory is publishable, and the original never leaves home.
- Trajectories from real users may enter local training, but are never published.

## 15. Evaluation before production

Three layers:

1. **Deterministic checks:** the citation exists and the passage is in the document; code compiles and passes tests; correct refusal outside the domain.
2. **Claude as judge** (only in the validation phase): correctness, completeness and grounding, with fixed criteria.
3. **Human review by sampling**, especially where the judge and the rules disagree.

Golden test set per domain, never used in training. A new adapter only goes into production if it matches or beats the previous one in every domain. Saved versions and one-click rollback.

## 16. Model manager and priorities

- Fixed memory budget on the Mac (e.g. 22 of 32 GB); the rest is left for macOS and training.
- Part of the budget is reserved for the administrator.
- Model states: fixed resident, warm, cold, unloading. The model unused for the longest time and with the lowest priority leaves first.
- **Administrator:** never refused, and may take models away from other sessions.
- **Users** (about three at the same time): only load models if they fit in the free space; otherwise they wait in the queue or are served in degraded mode by the generalist, but are never left without an answer.
- Sessions per user in Postgres; the harness (Hermes or another) only sends the session identifier; changing the subject does not lose context.
- Requests from several users are batched; each one has its own context cache.

Estimate of memory with everything resident (router, specialist base, adapters, aggregator, StarCoder2): about 11 GB.

## 17. Watchdog

- A tiny, separate process, always alive (e.g. via launchd on macOS), which survives even if the rest falls over.
- Deterministic core, no model: reads memory pressure, swap, temperature and queue latency.
- Acts when something leaves the expected range: unloads cold models, pauses training, blocks new loads, restarts stuck workers.
- Optionally, a small model on top only to explain the incidents on the dashboard.
- Runs locally on each node: the protection of the machine does not depend on the network.

## 18. Distribution: control plane and nodes

- **Control plane** (hypervisor or data server): Ariadne, Postgres, backend, sessions, model manager, priorities, dashboard, sandbox.
- **Inference nodes** (Mac Studio; more Macs in the future): a simple model server, over HTTP, that does not know about the rest of the network.
- Each node also exposes an API to load and unload models (llama-swap for llama.cpp; a thin custom layer for MLX).
- Registry of specialists with name, domain and address; adding a Mac means registering more addresses, without changing code.
- Real parallel fan-out with `asyncio`; on a single Mac the specialists share the GPU, and real parallelism comes with more nodes.
- The router may run on the data server if the non-AVX benchmark runs well; otherwise it runs on the Mac.
- If a node goes down, it is marked offline and the system answers in degraded mode (search in Ariadne only).

## 19. Optional cloud

- Off by default, enabled by the administrator, with priority below the local nodes. The system never depends on it.
- Main use: training and heavy curation batches.
- Planned built-in support: **Kaggle** (about 30 hours of free GPU per week), **Google Colab**, **RunPod**, **Vast.ai**.
- The free tiers are not suitable for permanent inference (terms of use and sessions that drop).
- Inference on rented GPUs only through a tunnel (Tailscale/WireGuard), and with the awareness that the data leaves home.

## 20. Approved models and licences

Each model is stored with its original licence file alongside it, and with a visible label on the dashboard. The project does not claim ownership of any model: it passes each one on with its own licence. Adapters inherit the licence of the base model. Project rule: non-commercial use.

Record per model: name, manufacturer, country of origin, licence, usage restrictions, role in the system, fingerprint of the weights.

| Model | Origin | Licence | Role |
| --- | --- | --- | --- |
| SmolLM2-360M | Hugging Face | Apache 2.0 | Specialist base |
| SmolLM2-1.7B | Hugging Face | Apache 2.0 | Aggregator |
| StarCoder2-3B | BigCode (HF + ServiceNow) | OpenRAIL-M | Code generation |
| Gemma 3 12B | Google | Gemma terms | Curator |
| multilingual-e5-small | Microsoft | MIT | Embeddings |
| open-jev | Independent project | To be confirmed | Router |
| LibreTranslate / Argos | Community | AGPL | Translation |

Excluded: Qwen, bge (BAAI) and any other Chinese model; NLLB (non-commercial licence that does not suit this project).

## 21. Backups

- Everything lives on the data server, covered by the data server's own backup policy.
- **Weekly external release on GitHub**, with files split into parts of at most 2 GB, encrypted before sending (age or GPG, key kept outside the system).
- Each release carries a manifest with the list of parts, fingerprints and a restore script. The restore is rehearsed periodically.
- Content: Wiki.js content, adapters, training logs of the `trainer` user, the list of approved models, configuration.
- **Never go to GitHub:** data from real users (sessions, API keys, query logs).
- Left out (can be downloaded again): base model weights and ZIMs.
- Suggested retention: the last eight releases.

## 22. Dashboard

Everything must be defined, stored and visible:

- **Models:** loaded and in what state, memory per model and per session, queue, licence; the administrator can pin or unload by hand.
- **System health:** watchdog metrics and incidents, state of the nodes.
- **Curation:** state of each article, flags triggered, curator decision, fidelity check, human review queue, link to the original ZIM.
- **Training (full lineage):** for each training run, the pipeline, the data used, the test results, what failed and why, the history; for each question, the trajectory, the citations, whether it passed the verification and in which trainings it was used.
- **Evaluation:** results per domain, comparison between versions, rollback.
