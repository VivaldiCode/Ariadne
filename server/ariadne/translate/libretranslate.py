"""LibreTranslate (https://libretranslate.com): tradução automática local e offline (Argos).

`POST {url}/translate` com `{q, source, target, format: "text", api_key?}`. O Argos tende para o
português do Brasil; para PT-PT aponta-se `LIBRETRANSLATE_PT_CODE` para um modelo próprio (ver o
`ARCHITECTURE.md`, secção 8)."""

import contextlib

import httpx

from ariadne.translate.base import TranslateError, TranslateUnavailable


class LibreTranslate:
    def __init__(
        self,
        url: str,
        *,
        api_key: str = "",
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.name = f"libretranslate:{url.rstrip('/')}"
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)

    def translate(
        self, text: str, source: str, target: str, *, timeout: float | None = None
    ) -> str:
        payload: dict[str, str] = {"q": text, "source": source, "target": target, "format": "text"}
        if self._api_key:
            payload["api_key"] = self._api_key
        try:
            response = self._client.post(
                f"{self._url}/translate", json=payload, timeout=timeout or self._timeout
            )
        except httpx.HTTPError as exc:
            raise TranslateUnavailable(
                f"LibreTranslate unreachable ({type(exc).__name__})"
            ) from exc
        if response.status_code != 200:
            detail = ""
            with contextlib.suppress(ValueError, AttributeError):
                detail = str(response.json().get("error", ""))
            error = TranslateUnavailable if response.status_code >= 500 else TranslateError
            raise error(
                f"LibreTranslate responded {response.status_code}"
                + (f": {detail}" if detail else "")
            )
        try:
            translated = response.json()["translatedText"]
        except (ValueError, KeyError, TypeError) as exc:
            raise TranslateError("Unexpected response from LibreTranslate") from exc
        if not isinstance(translated, str):
            raise TranslateError("Unexpected response from LibreTranslate")
        return translated

    def close(self) -> None:
        self._client.close()
