"""Publicar a release no GitHub (API REST, sem dependências novas): `ariadne release publish`.

**Desligado por omissão** (`RELEASE_ENABLED=false`): só publica com a variável ligada, e
`--dry-run` nunca toca na rede. Publica sempre como rascunho, envia as partes e só no fim torna a
release visível, por isso uma falha a meio nunca deixa uma release pública incompleta (e voltar a
correr retoma o mesmo rascunho). Antes de publicar volta a verificar as impressões digitais.
Retenção: ficam as últimas `RELEASE_RETENTION` (8) releases `ariadne-release-*`; só estas se
apagam, nunca outras releases do repositório.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ariadne.config import Settings
from ariadne.release.pack import MANIFEST, RESTORE, SIGNATURE, SUMS
from ariadne.release.verify import check_fingerprints, load_manifest

TAG_PREFIX = "ariadne-release-"
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# O repositório do código do projeto: nunca é destino de uma release de conteúdo (nem por engano
# de configuração num deploy).
SOURCE_REPO_URL = "https://github.com/VivaldiCode/Ariadne"
SOURCE_REPO = SOURCE_REPO_URL.removeprefix("https://github.com/").lower()
ASSET_CHUNK = 4 * 1024 * 1024


class PublishError(Exception):
    pass


class ReleaseDisabled(PublishError):
    """RELEASE_ENABLED=false: a publicação está desligada."""


@dataclass
class PublishResult:
    dry_run: bool
    tag: str
    assets: list[tuple[str, int]]
    url: str | None = None
    uploaded: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(size for _, size in self.assets)


def assets_of(directory: Path) -> list[tuple[str, int]]:
    manifest = load_manifest(directory)
    names = [p["name"] for p in manifest["parts"]] + [MANIFEST, SIGNATURE, SUMS, RESTORE]
    return [(name, (directory / name).stat().st_size) for name in names]


def _chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(ASSET_CHUNK):
            yield chunk


def _check(response: httpx.Response, what: str) -> Any:
    if response.status_code >= 400:
        raise PublishError(f"GitHub: {what} returned {response.status_code}: {response.text[:200]}")
    return response.json() if response.content else {}


def _paged(client: httpx.Client, path: str) -> Iterator[dict[str, Any]]:
    page = 1
    while True:
        batch = _check(client.get(path, params={"per_page": 100, "page": page}), f"GET {path}")
        yield from batch
        if len(batch) < 100:
            return
        page += 1


def publish(
    settings: Settings,
    directory: Path,
    *,
    dry_run: bool,
    client: httpx.Client | None = None,
) -> PublishResult:
    report = check_fingerprints(directory, settings.release_signing_pubkey)
    if not report.ok:
        failed = "; ".join(f"{c.name}: {c.detail}" for c in report.checks if not c.ok)
        raise PublishError(f"The release does not pass fingerprint verification ({failed}).")
    manifest = load_manifest(directory)
    tag = directory.name
    if not tag.startswith(TAG_PREFIX):
        raise PublishError(f"The folder name must start with {TAG_PREFIX!r}.")
    if settings.release_github_repo.strip().lower() == SOURCE_REPO:
        raise PublishError(
            f"RELEASE_GITHUB_REPO points to the project's source repository ({SOURCE_REPO_URL}): "
            "content releases are never published there."
        )
    result = PublishResult(dry_run=dry_run, tag=tag, assets=assets_of(directory))
    if dry_run:
        repo_name = settings.release_github_repo or "(RELEASE_GITHUB_REPO not set)"
        result.notes = [
            f"[dry-run] would create release {tag} in {repo_name}",
            f"[dry-run] would upload {len(result.assets)} files ({result.total_bytes} bytes)",
            f"[dry-run] would then delete the {TAG_PREFIX}* releases beyond the latest "
            f"{settings.release_retention}",
        ]
        return result
    if not settings.release_enabled:
        raise ReleaseDisabled(
            "Publishing is off (RELEASE_ENABLED=false). Use --dry-run to rehearse."
        )
    if not REPO.fullmatch(settings.release_github_repo):
        raise PublishError('RELEASE_GITHUB_REPO must be "owner/repository".')
    if not settings.release_github_token:
        raise PublishError("RELEASE_GITHUB_TOKEN is missing.")
    api = settings.release_github_api_url.rstrip("/")
    own = client is None
    http = client or httpx.Client(
        base_url=api,
        timeout=settings.release_github_timeout_seconds,
        headers={
            "Authorization": f"Bearer {settings.release_github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ariadne-release",
        },
    )
    repo = f"/repos/{settings.release_github_repo}"
    try:
        existing = next(
            (r for r in _paged(http, f"{repo}/releases") if r.get("tag_name") == tag), None
        )
        if existing is None:
            existing = _check(
                http.post(
                    f"{repo}/releases",
                    json={
                        "tag_name": tag,
                        "name": tag,
                        "draft": True,
                        "prerelease": False,
                        "body": (
                            "Encrypted Ariadne release (age): open-class content only, "
                            f"in {len(manifest['parts'])} part(s). Restore: `restore.sh` "
                            "(needs the private key) and `MANIFEST.json` with the "
                            "fingerprints."
                        ),
                    },
                ),
                "create the release",
            )
        elif not existing.get("draft"):
            raise PublishError(f"Release {tag} is already published.")
        upload_url = str(existing["upload_url"]).split("{")[0]
        allowed = {urlparse(api).hostname, "uploads.github.com"}
        if urlparse(upload_url).hostname not in allowed:
            raise PublishError("The release upload URL points to an unexpected host.")
        # Ao retomar, só se dá por enviado o ficheiro que está `uploaded` e com o tamanho certo:
        # um envio cortado a meio apaga-se e volta a enviar-se.
        have: dict[str, dict[str, Any]] = {a["name"]: a for a in existing.get("assets", [])}
        for name, size in result.assets:
            asset = have.get(name)
            if asset is not None:
                if asset.get("state", "uploaded") == "uploaded" and asset.get("size") == size:
                    continue
                _check(
                    http.delete(f"{repo}/releases/assets/{asset['id']}"),
                    f"delete the incomplete upload of {name}",
                )
            _check(
                http.post(
                    upload_url,
                    params={"name": name},
                    content=_chunks(directory / name),
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(size),
                    },
                ),
                f"upload {name}",
            )
            result.uploaded.append(name)
        published = _check(
            http.patch(f"{repo}/releases/{existing['id']}", json={"draft": False}),
            "publish the release",
        )
        result.url = published.get("html_url") or existing.get("html_url")
        result.deleted = _apply_retention(http, repo, settings.release_retention, keep=tag)
    finally:
        if own:
            http.close()
    return result


def _apply_retention(http: httpx.Client, repo: str, keep_last: int, *, keep: str) -> list[str]:
    """Só as `ariadne-release-*`. Os rascunhos órfãos (envios que ficaram a meio e não são esta
    release) apagam-se e **não contam** para as últimas `keep_last`; das publicadas ficam as mais
    recentes."""
    ours = [
        r
        for r in _paged(http, f"{repo}/releases")
        if str(r.get("tag_name", "")).startswith(TAG_PREFIX) and r.get("tag_name") != keep
    ]
    orphans = [r for r in ours if r.get("draft")]
    published = sorted(
        (r for r in ours if not r.get("draft")), key=lambda r: str(r["tag_name"]), reverse=True
    )
    # a que acabou de sair já é uma das `keep_last`: das outras ficam `keep_last - 1`
    doomed = orphans + published[max(keep_last - 1, 0) :]
    deleted: list[str] = []
    for old in doomed:
        _check(http.delete(f"{repo}/releases/{old['id']}"), f"delete {old['tag_name']}")
        ref = http.delete(f"{repo}/git/refs/tags/{old['tag_name']}")
        if ref.status_code not in (204, 404, 422):
            raise PublishError(f"GitHub: deleting tag {old['tag_name']} returned {ref.status_code}")
        deleted.append(str(old["tag_name"]))
    return deleted
