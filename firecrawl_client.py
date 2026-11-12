"""Async client for a self-hosted Firecrawl instance.

The module only depends on aiohttp and deliberately imports nothing from
AstrBot, so it can be tested (or reused) outside of the bot.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Sequence

import aiohttp

DEFAULT_BASE_URL = "http://127.0.0.1:3002"
DEFAULT_TIMEOUT = 60.0

#: Status codes worth retrying with the next API key.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
#: Status codes that mean "this key may not do that".
AUTH_STATUS = frozenset({401, 402, 403})
#: Responses that usually mean the endpoint does not exist on this API version.
MISSING_ENDPOINT_STATUS = frozenset({404, 405, 501})


class FirecrawlError(RuntimeError):
    """Raised when a Firecrawl request cannot be completed."""


class FirecrawlConnectionError(FirecrawlError):
    """Raised when the configured instance cannot be reached at all."""


@dataclass(slots=True)
class SearchHit:
    """One normalized search result."""

    title: str
    url: str
    snippet: str = ""
    markdown: str = ""


def normalize_base_url(base_url: str | None) -> str:
    """Normalize a user supplied base URL.

    Args:
        base_url: Base URL such as http://127.0.0.1:3002/ or
            https://firecrawl.example.com/v2.

    Returns:
        The URL without trailing slashes and without a trailing API version,
        because the client appends /v1 or /v2 itself.
    """
    url = (base_url or DEFAULT_BASE_URL).strip()
    if not url:
        url = DEFAULT_BASE_URL
    if "://" not in url:
        url = "http://" + url
    url = url.rstrip("/")
    for suffix in ("/v1", "/v2"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url.rstrip("/")


def _error_message(body: Any, status: int) -> str:
    """Build a readable error message from a Firecrawl error response."""
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("error") or body.get("message") or "").strip()
    elif isinstance(body, str):
        detail = body.strip()[:300]
    if not detail:
        detail = "HTTP %s" % status
    elif not detail.startswith("HTTP"):
        detail = "HTTP %s: %s" % (status, detail)
    return detail


def normalize_search_results(data: Any) -> list[SearchHit]:
    """Normalize the data field of a search response.

    Firecrawl v2 answers with {"web": [...]} while v1 answers with a plain
    list, so both shapes are accepted.

    Args:
        data: The data field of the API response.

    Returns:
        Normalized hits; entries without a URL are skipped.
    """
    rows: Any = data
    if isinstance(data, dict):
        for key in ("web", "results", "news"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
        else:
            rows = []
    if not isinstance(rows, list):
        return []

    hits: list[SearchHit] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        hits.append(
            SearchHit(
                title=str(item.get("title") or url).strip(),
                url=url,
                snippet=str(
                    item.get("description")
                    or item.get("snippet")
                    or item.get("markdown")
                    or ""
                ).strip(),
                markdown=str(item.get("markdown") or ""),
            )
        )
    return hits


class FirecrawlClient:
    """Client for the /v2 (falling back to /v1) Firecrawl API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_keys: Sequence[str] | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        api_version: str = "auto",
        verify_ssl: bool = True,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.api_keys = [key.strip() for key in (api_keys or []) if str(key).strip()]
        self.timeout = max(1.0, float(timeout))
        self.api_version = (api_version or "auto").strip().lower()
        self.verify_ssl = verify_ssl
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._key_lock = asyncio.Lock()
        self._key_index = 0
        self._resolved_version: str | None = (
            self.api_version if self.api_version in ("v1", "v2") else None
        )

    @property
    def resolved_version(self) -> str | None:
        """API version actually in use, or None before the first success."""
        return self._resolved_version

    async def __aenter__(self) -> "FirecrawlClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                connector = aiohttp.TCPConnector(ssl=self.verify_ssl)
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    connector=connector,
                    trust_env=True,
                )
        return self._session

    async def _keys(self) -> list[str | None]:
        """Return the key list rotated round-robin, so keys are reused fairly."""
        keys: list[str | None] = list(self.api_keys) or [None]
        async with self._key_lock:
            offset = self._key_index % len(keys)
            self._key_index += 1
        return keys[offset:] + keys[:offset]

    def _versions(self) -> list[str]:
        if self._resolved_version:
            return [self._resolved_version]
        return ["v2", "v1"]

    @staticmethod
    async def _decode(response: aiohttp.ClientResponse) -> Any:
        text = await response.text()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"error": text[:300]}

    async def _request(self, path: str, payload: dict[str, Any]) -> Any:
        """POST payload to path and return the data field.

        Tries every configured API key and, while the version is still
        unknown, every known API version. Authentication and retryable errors
        fail over to the next key; other 4xx responses are final.
        """
        session = await self._get_session()
        last_error: FirecrawlError | None = None
        candidates = await self._keys()

        for key in candidates:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            if key:
                headers["Authorization"] = "Bearer " + key

            versions = self._versions()
            for position, version in enumerate(versions):
                url = "%s/%s%s" % (self.base_url, version, path)
                try:
                    async with session.post(url, json=payload, headers=headers) as resp:
                        body = await self._decode(resp)
                        if (
                            resp.status in MISSING_ENDPOINT_STATUS
                            and position + 1 < len(versions)
                        ):
                            last_error = FirecrawlError(
                                "/%s%s not available: %s"
                                % (version, path, _error_message(body, resp.status))
                            )
                            continue
                        if (
                            resp.status in AUTH_STATUS
                            or resp.status in RETRYABLE_STATUS
                        ):
                            last_error = FirecrawlError(
                                _error_message(body, resp.status)
                            )
                            break
                        if resp.status >= 400:
                            raise FirecrawlError(_error_message(body, resp.status))
                        if isinstance(body, dict) and body.get("success") is False:
                            raise FirecrawlError(_error_message(body, resp.status))
                        self._resolved_version = version
                        if isinstance(body, dict):
                            return body.get("data", body)
                        return body
                except FirecrawlConnectionError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    raise FirecrawlConnectionError(
                        "无法连接 Firecrawl 实例 %s（%s: %s）"
                        % (self.base_url, exc.__class__.__name__, exc)
                    ) from exc

        if last_error is not None:
            raise last_error
        raise FirecrawlError("Firecrawl 请求失败：没有可用的 API Key。")

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        sources: Sequence[str] | None = None,
        location: str | None = None,
        country: str | None = None,
        timeout_ms: int | None = None,
        scrape_formats: Sequence[str] | None = None,
    ) -> list[SearchHit]:
        """Run a web search.

        Args:
            query: Search keywords.
            limit: Number of results, clamped to 1..100.
            sources: Firecrawl sources, ["web"] by default.
            location: Optional geographic bias.
            country: Optional two letter country code.
            timeout_ms: Per request timeout in milliseconds.
            scrape_formats: When given, ask Firecrawl to also scrape each hit.

        Returns:
            Normalized hits.
        """
        payload: dict[str, Any] = {
            "query": query,
            "limit": max(1, min(int(limit), 100)),
            "sources": list(sources or ["web"]),
        }
        if location:
            payload["location"] = location
        if country:
            payload["country"] = country
        if timeout_ms:
            payload["timeout"] = int(timeout_ms)
        if scrape_formats:
            payload["scrapeOptions"] = {"formats": list(scrape_formats)}

        data = await self._request("/search", payload)
        return normalize_search_results(data)

    async def scrape(
        self,
        url: str,
        *,
        formats: Sequence[str] | None = None,
        only_main_content: bool = True,
        timeout_ms: int | None = None,
        max_age: int | None = None,
    ) -> dict[str, Any]:
        """Scrape one URL.

        Args:
            url: Page to read.
            formats: Output formats, ["markdown"] by default.
            only_main_content: Strip navigation and boilerplate.
            timeout_ms: Per request timeout in milliseconds.
            max_age: Maximum cache age in milliseconds.

        Returns:
            The Firecrawl data object, containing at least one requested
            format key (for example markdown).
        """
        payload: dict[str, Any] = {
            "url": url,
            "formats": list(formats or ["markdown"]),
            "onlyMainContent": bool(only_main_content),
        }
        if timeout_ms:
            payload["timeout"] = int(timeout_ms)
        if max_age:
            payload["maxAge"] = int(max_age)

        data = await self._request("/scrape", payload)
        return data if isinstance(data, dict) else {"result": data}

    async def ping(self) -> dict[str, Any]:
        """Check that the instance answers HTTP requests.

        Returns:
            The parsed root response, or a small dict describing it.
        """
        session = await self._get_session()
        url = self.base_url + "/"
        try:
            async with session.get(url) as resp:
                body = await self._decode(resp)
                if resp.status >= 400:
                    raise FirecrawlError(_error_message(body, resp.status))
                if isinstance(body, dict):
                    return body
                return {"message": str(body)[:200]}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise FirecrawlConnectionError(
                "无法连接 Firecrawl 实例 %s（%s: %s）"
                % (self.base_url, exc.__class__.__name__, exc)
            ) from exc
