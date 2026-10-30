"""Um GitHub falso e local (API de releases), para testar `release publish` sem rede."""

import hashlib
import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

REPO = "dono/repo"
TOKEN = "ghp_token_de_teste"


class FakeGitHub:
    def __init__(self) -> None:
        self.releases: dict[int, dict[str, Any]] = {}
        self.next_id = 1
        self.next_asset = 100
        self.requests: list[tuple[str, str]] = []
        self.port = 0
        self.fail_upload_of: str | None = None
        self.tags_deleted: list[str] = []
        self.assets_deleted: list[int] = []

    @property
    def base(self) -> str:
        return f"http://localhost:{self.port}"

    def seed(self, tag: str, *, draft: bool = False) -> int:
        release_id = self.next_id
        self.next_id += 1
        self.releases[release_id] = {
            "id": release_id,
            "tag_name": tag,
            "name": tag,
            "draft": draft,
            "assets": [],
            "html_url": f"{self.base}/{REPO}/releases/tag/{tag}",
            "upload_url": (
                f"{self.base}/upload/repos/{REPO}/releases/{release_id}/assets{{?name,label}}"
            ),
        }
        return release_id


def _handler(fake: FakeGitHub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _json(self, code: int, body: Any = None) -> None:
            raw = b"" if body is None else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _auth(self) -> bool:
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._json(401, {"message": "Bad credentials"})
                return False
            return True

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length", 0))
            data = b""
            while len(data) < length:
                chunk = self.rfile.read(min(1 << 20, length - len(data)))
                if not chunk:
                    break
                data += chunk
            return data

        def do_GET(self) -> None:
            fake.requests.append(("GET", self.path))
            if not self._auth():
                return
            url = urlparse(self.path)
            if url.path == f"/repos/{REPO}/releases":
                query = parse_qs(url.query)
                page, per = int(query.get("page", ["1"])[0]), int(query.get("per_page", ["30"])[0])
                items = list(fake.releases.values())
                self._json(200, items[(page - 1) * per : page * per])
            else:
                self._json(404, {"message": "Not Found"})

        def do_POST(self) -> None:
            fake.requests.append(("POST", self.path))
            if not self._auth():
                return
            url = urlparse(self.path)
            body = self._body()
            if url.path == f"/repos/{REPO}/releases":
                payload = json.loads(body)
                release_id = fake.seed(payload["tag_name"], draft=bool(payload.get("draft")))
                fake.releases[release_id]["body"] = payload.get("body")
                self._json(201, fake.releases[release_id])
            elif m := re.fullmatch(rf"/upload/repos/{REPO}/releases/(\d+)/assets", url.path):
                name = parse_qs(url.query)["name"][0]
                if fake.fail_upload_of == name:
                    self._json(500, {"message": "boom"})
                    return
                release = fake.releases[int(m.group(1))]
                fake.next_asset += 1
                release["assets"].append(
                    {
                        "id": fake.next_asset,
                        "name": name,
                        "size": len(body),
                        "state": "uploaded",
                        "sha256": hashlib.sha256(body).hexdigest(),
                    }
                )
                self._json(201, release["assets"][-1])
            else:
                self._json(404, {"message": "Not Found"})

        def do_PATCH(self) -> None:
            fake.requests.append(("PATCH", self.path))
            if not self._auth():
                return
            body = json.loads(self._body() or b"{}")
            if m := re.fullmatch(rf"/repos/{REPO}/releases/(\d+)", self.path):
                release = fake.releases[int(m.group(1))]
                release.update(body)
                self._json(200, release)
            else:
                self._json(404, {"message": "Not Found"})

        def do_DELETE(self) -> None:
            fake.requests.append(("DELETE", self.path))
            if not self._auth():
                return
            if m := re.fullmatch(rf"/repos/{REPO}/releases/(\d+)", self.path):
                fake.releases.pop(int(m.group(1)), None)
                self._json(204)
            elif m := re.fullmatch(rf"/repos/{REPO}/releases/assets/(\d+)", self.path):
                for release in fake.releases.values():
                    release["assets"] = [a for a in release["assets"] if a["id"] != int(m.group(1))]
                fake.assets_deleted.append(int(m.group(1)))
                self._json(204)
            elif m := re.fullmatch(rf"/repos/{REPO}/git/refs/tags/(.+)", self.path):
                fake.tags_deleted.append(m.group(1))
                self._json(204)
            else:
                self._json(404, {"message": "Not Found"})

    return Handler


@contextmanager
def serve_github(fake: FakeGitHub) -> Iterator[FakeGitHub]:
    server = ThreadingHTTPServer(("localhost", 0), _handler(fake))
    fake.port = server.server_address[1]
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
