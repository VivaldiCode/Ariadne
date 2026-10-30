"""Cliente da API REST do Jobe (https://github.com/trampgeek/jobe).

O Jobe corre o código em utilizadores descartáveis (`jobeNN`) com limites de tempo, memória e
processos impostos pelo runguard. Em produção está numa máquina à parte (hipervisor) e, no compose,
numa rede sem saída para a internet: este cliente só precisa do URL.
"""

from typing import Any

import httpx

from ariadne.sandbox.base import (
    Limits,
    Outcome,
    RunResult,
    Sandbox,
    SandboxError,
    SandboxUnavailable,
)

API = "/jobe/index.php/restapi"

# Limites de omissão por linguagem (o Jobe tem os seus, mas o V8 do Node precisa de vários GB de
# espaço de endereçamento virtual só para arrancar: com 1 GB aborta com «Failed to reserve virtual
# memory for CodeRange»). A memória é virtual (RLIMIT_AS), não a que o processo usa de facto.
DEFAULT_LIMITS: dict[str, Limits] = {
    "nodejs": Limits(cpu_seconds=10, memory_mb=4096),
    "python3": Limits(cpu_seconds=5, memory_mb=400),
}
FALLBACK_LIMITS = Limits(cpu_seconds=5)
EXTENSIONS = {"python3": "py", "nodejs": "js", "c": "c", "cpp": "cpp", "java": "java", "php": "php"}


def _parameters(language: str, limits: Limits | None) -> dict[str, int]:
    base = DEFAULT_LIMITS.get(language, FALLBACK_LIMITS)
    chosen = limits or Limits()
    merged = {
        "cputime": chosen.cpu_seconds or base.cpu_seconds,
        "memorylimit": chosen.memory_mb or base.memory_mb,
        "numprocs": chosen.processes or base.processes,
        "disklimit": chosen.disk_mb or base.disk_mb,
        "streamsize": chosen.stream_kb or base.stream_kb,
    }
    return {key: value for key, value in merged.items() if value}


class JobeSandbox(Sandbox):
    def __init__(
        self,
        url: str,
        *,
        api_key: str = "",
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base = url.rstrip("/") + API
        headers = {"X-API-KEY": api_key} if api_key else {}
        self._client = client or httpx.Client(timeout=timeout, headers=headers)
        self._owns_client = client is None

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, self._base + path, **kwargs)
        except httpx.HTTPError as exc:
            raise SandboxUnavailable(f"Jobe unavailable ({type(exc).__name__}: {exc})") from exc
        if response.status_code >= 500:
            raise SandboxUnavailable(f"Jobe answered {response.status_code}: {response.text[:200]}")
        return response

    def languages(self) -> dict[str, str]:
        response = self._request("GET", "/languages")
        if response.status_code != 200:
            raise SandboxError(f"Jobe: languages returned {response.status_code}")
        return {str(name): str(version) for name, version in response.json()}

    def run(
        self,
        language: str,
        source: str,
        *,
        stdin: str = "",
        limits: Limits | None = None,
        filename: str | None = None,
    ) -> RunResult:
        extension = EXTENSIONS.get(language, "txt")
        spec: dict[str, Any] = {
            "language_id": language,
            "sourcefilename": filename or f"prog.{extension}",
            "sourcecode": source,
            "input": stdin,
            "parameters": _parameters(language, limits),
        }
        response = self._request("POST", "/runs", json={"run_spec": spec})
        if response.status_code == 400:
            raise SandboxError(f"Jobe rejected the request: {response.text[:300]}")
        if response.status_code not in (200, 202):
            raise SandboxError(f"Jobe: unexpected response {response.status_code}")
        body = response.json()
        try:
            outcome = Outcome(int(body["outcome"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise SandboxError(f"Jobe: unreadable result: {str(body)[:200]}") from exc
        return RunResult(
            outcome=outcome,
            stdout=str(body.get("stdout") or ""),
            stderr=str(body.get("stderr") or ""),
            compile_output=str(body.get("cmpinfo") or ""),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
