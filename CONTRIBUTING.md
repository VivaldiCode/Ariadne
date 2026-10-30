# Contributing to Ariadne

Thanks for helping out. This document covers what you need to get a change into the project and what happens afterwards.

## Before you start

- Read the [architecture document](ARCHITECTURE.md). It is the source of truth. If a decision there looks wrong or
  incomplete to you, open an issue to discuss it first. A change that alters a decision must update the document in the
  same pull request.
- For anything large (a new service, a native dependency, a data source), open an issue before writing code.
- Security vulnerabilities go through [`SECURITY.md`](SECURITY.md), never a public issue.
- This project follows the [code of conduct](CODE_OF_CONDUCT.md).

## Reporting bugs

Open an issue with:

- what you did, what you expected and what happened instead;
- the version or commit, and how you run Ariadne (the standalone compose, or your own setup);
- relevant log output (`docker compose logs <service>`), with secrets and private content removed;
- the smallest reproduction you can manage (a command, a request, a small ZIM).

Feature requests are welcome too; say what problem you are solving, not only the feature you have in mind.

## Project requirements

- **CPU only, no AVX.** The production target is an old x86-64 CPU (Xeon X5550: SSE4.2 only), and a GPU, if present,
  does not count. No native dependencies that assume AVX/AVX2/FMA (PyTorch stays out of the `server`); llama.cpp is built
  without `GGML_NATIVE`. A new native dependency has to pass the `bench/embeddings` canary before it enters the `server`.
- **No models of Chinese origin**, neither in the `server` nor in the external GPU batch; a prefix guard rejects them.
- **Dependencies sit behind interfaces** (`Source`, `Embedder`, `SemanticStore`, `WikiBackend`).
- **Licenses.** Every piece of content has a license class (open or private), and private content never leaves in a
  release (`assert_exportable`). Do not work around this.
- **Secrets** come only from environment variables or the integrations vault; never from code, compose files or tests.

## Development setup

You need Python 3.12 with [`uv`](https://docs.astral.sh/uv/), Node 22+ for the web UI, and Docker Compose v2.

```bash
docker compose up -d postgres          # integration tests run against the real Postgres
deploy/kiwix/get-test-zim.sh           # a small ZIM for the tests
cd server && uv sync                   # Python dependencies
cd ../web && npm ci                    # web dependencies
```

Configuration is by environment variables (`pydantic-settings`), using the same names as `docker-compose.yml`.

## Running the tests and checks

```bash
cd server
uv run ruff check . && uv run ruff format --check .
uv run mypy ariadne                    # strict
uv run pytest                          # integration tests need the Postgres and the test ZIM above

cd ../web
npm run typecheck && npm test          # TypeScript and vitest
```

Every behavior change comes with tests. CI runs the same checks on your pull request.

## Code style

- Python: `ruff` (lint and format), `mypy` in strict mode on `server/ariadne`, tests with `pytest`.
- Web: TypeScript, tested with `vitest`.
- Follow the style of the surrounding code.
- Code comments and some internal docstrings are still in Portuguese, until further notice. You do not need to translate
  them, and contributions written in English are welcome and accepted as they are. Identifiers, commit messages, issues
  and pull requests should be in English.

## Translations and the UI language

The web UI is translated through message catalogs in `web/src/i18n/{en,pt}/`, one folder per language. User-facing text
belongs in the catalogs, not inline in components. To add or improve a language, copy the `en` catalog, translate the
values (not the keys) and keep the same structure; the tests fail on missing keys. English is the reference language.

## Commit and pull request flow

- Keep commits small, with a message that says why, not only what.
- Sign every commit with `git commit -s` (see "License of contributions" below).
- A decision change includes the matching update to `ARCHITECTURE.md`.
- Open the pull request from a fork, with tests and a description of the reason for the change.

## License of contributions

The project is [AGPL-3.0](LICENSE). By submitting a contribution, you agree that it falls under the same license. We ask
for the [Developer Certificate of Origin](https://developercertificate.org): a `Signed-off-by:` line on each commit
(`git commit -s`) states that you have the right to submit it.

## How a contribution reaches the project

This public repository is **generated** from the maintainer's development repository, which remains the source of truth:
with each release, the public tree is exported as a single commit. Therefore:

1. Open your pull request against this repository, from a fork, with tests and a description of why.
2. The pull request's CI has to pass; a maintainer reviews it here.
3. Once accepted, the maintainer applies your changes to the development repository **preserving your authorship** (your
   name and email on the commits, and the `Signed-off-by`), and the pull request is closed here with a link to that commit.
4. Your changes reappear in this repository in the next release, inside the exported tree.

What this means for you: this repository's history has one commit per release (your authorship lives in the commit in the
development repository and in the contributor list of the release notes), and **nothing is merged directly into the main
branch here** other than release commits, because the next export would overwrite it; the export refuses to publish over
changes that have not yet been imported.
