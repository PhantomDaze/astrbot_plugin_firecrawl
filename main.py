"""AstrBot plugin: turn a self-hosted Firecrawl instance into LLM tools.

The plugin mirrors what AstrBot ships for the hosted Firecrawl service
(web search plus page extraction) but sends every request to the instance
configured in the plugin settings, so a self-hosted deployment can be used
from chat.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .firecrawl_client import (
    DEFAULT_BASE_URL,
    FirecrawlClient,
    FirecrawlConnectionError,
    FirecrawlError,
)

#: Output formats Firecrawl can return for a scraped page.
SUPPORTED_FORMATS = ("markdown", "html", "rawHtml", "summary")

#: Hard cap for chat replies of the /firecrawl command.
COMMAND_REPLY_LIMIT = 1500


def _truncate(text: str, limit: int) -> str:
    """Cut text down to limit characters, marking that it was shortened."""
    if limit and limit > 0 and len(text) > limit:
        return "%s\n\n...[内容已截断，原文共 %d 字符]" % (text[:limit], len(text))
    return text


def _as_int(value: Any, default: int) -> int:
    """Best effort integer conversion for values coming from the model."""
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    """Best effort boolean conversion for values coming from the model."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    return bool(value)


def _split_command_args(message_str: str) -> list[str]:
    """Split '/firecrawl search cats' into ['search', 'cats']."""
    text = (message_str or "").strip()
    while text[:1] in ("/", "／", "!"):
        text = text[1:].lstrip()
    parts = text.split()
    if parts and parts[0].lower() in ("firecrawl", "fc"):
        parts = parts[1:]
    return parts


class FirecrawlPlugin(Star):
    """用自建 Firecrawl 实例为 AstrBot 提供联网搜索与网页正文提取。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.client = self._build_client()

    # ------------------------------------------------------------------
    # configuration helpers
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any) -> Any:
        """Read a config value, falling back when it is missing or null."""
        try:
            value = self.config.get(key, default)
        except AttributeError:
            return default
        return default if value is None else value

    def _api_keys(self) -> list[str]:
        """Collect configured API keys.

        Both the list field api_keys and a single api_key string are accepted;
        the list wins when it is not empty.
        """
        keys = self._cfg("api_keys", [])
        if isinstance(keys, str):
            keys = [keys]
        if not isinstance(keys, (list, tuple)):
            keys = []
        cleaned = [str(key).strip() for key in keys if str(key).strip()]
        if cleaned:
            return cleaned
        single = str(self._cfg("api_key", "")).strip()
        return [single] if single else []

    def _build_client(self) -> FirecrawlClient:
        return FirecrawlClient(
            base_url=str(self._cfg("base_url", DEFAULT_BASE_URL)),
            api_keys=self._api_keys(),
            timeout=float(_as_int(self._cfg("request_timeout", 60), 60)),
            api_version=str(self._cfg("api_version", "auto")),
            verify_ssl=_as_bool(self._cfg("verify_ssl", True), True),
        )

    def _content_limit(self) -> int:
        return max(0, _as_int(self._cfg("max_content_chars", 20000), 20000))

    def _default_format(self) -> str:
        value = str(self._cfg("default_format", "markdown")).strip()
        return value if value in SUPPORTED_FORMATS else "markdown"

    # ------------------------------------------------------------------
    # LLM tools
    # ------------------------------------------------------------------
    @filter.llm_tool(name="firecrawl_search")
    async def firecrawl_search(
        self,
        event: AstrMessageEvent,
        query: str,
        limit: int = 0,
        location: str = "",
        country: str = "",
        timeout: int = 0,
    ) -> str:
        """使用自建 Firecrawl 实例搜索互联网网页，返回标题、链接和摘要。

        Args:
            query(string): 必填。搜索关键词。
            limit(int): 可选。返回结果数量，1-20，留空使用插件配置的默认值。
            location(string): 可选。结果的地理位置偏好，例如 China。
            country(string): 可选。国家代码，例如 CN、US。
            timeout(int): 可选。单次请求超时毫秒数，留空使用插件配置。
        """
        query = str(query or "").strip()
        if not query:
            return "Error: query 不能为空。"

        configured_limit = _as_int(self._cfg("search_limit", 5), 5)
        count = _as_int(limit, configured_limit)
        count = max(1, min(count, 20))

        with_content = _as_bool(self._cfg("search_with_content", False), False)
        scrape_formats = [self._default_format()] if with_content else None

        try:
            hits = await self.client.search(
                query,
                limit=count,
                location=str(location or "").strip() or None,
                country=str(country or "").strip() or None,
                timeout_ms=_as_int(timeout, 0) or None,
                scrape_formats=scrape_formats,
            )
        except FirecrawlConnectionError as exc:
            return "Error: %s" % exc
        except FirecrawlError as exc:
            return "Error: Firecrawl 搜索失败：%s" % exc

        if not hits:
            return "Error: Firecrawl 没有返回任何搜索结果。"

        ref = uuid.uuid4().hex[:4]
        results: list[dict[str, Any]] = []
        for index, hit in enumerate(hits, 1):
            item: dict[str, Any] = {
                "title": hit.title,
                "url": hit.url,
                "snippet": _truncate(hit.snippet, 500),
                "index": "%s.%d" % (ref, index),
            }
            if with_content and hit.markdown:
                item["content"] = _truncate(hit.markdown, 4000)
            results.append(item)
        return json.dumps({"results": results}, ensure_ascii=False)

    @filter.llm_tool(name="firecrawl_scrape")
    async def firecrawl_scrape(
        self,
        event: AstrMessageEvent,
        url: str,
        format: str = "",
        only_main_content: bool = True,
        timeout: int = 0,
        max_age: int = 0,
    ) -> str:
        """读取指定网页的正文内容，用于总结、翻译或回答与该网页有关的问题。

        Args:
            url(string): 必填。要读取的网页地址，必须以 http 或 https 开头。
            format(string): 可选。返回格式，可选 markdown、html、rawHtml、summary，默认 markdown。
            only_main_content(boolean): 可选。是否只保留正文，默认 true。
            timeout(int): 可选。单次请求超时毫秒数，留空使用插件配置。
            max_age(int): 可选。允许使用缓存的毫秒数，留空表示由服务端决定。
        """
        url = str(url or "").strip()
        if not url:
            return "Error: url 不能为空。"
        if not url.lower().startswith(("http://", "https://")):
            return "Error: url 必须以 http:// 或 https:// 开头。"

        output_format = str(format or "").strip() or self._default_format()
        if output_format not in SUPPORTED_FORMATS:
            output_format = "markdown"

        try:
            result = await self.client.scrape(
                url,
                formats=[output_format],
                only_main_content=_as_bool(only_main_content, True),
                timeout_ms=_as_int(timeout, 0) or None,
                max_age=_as_int(max_age, 0) or None,
            )
        except FirecrawlConnectionError as exc:
            return "Error: %s" % exc
        except FirecrawlError as exc:
            return "Error: Firecrawl 网页提取失败：%s" % exc

        content = result.get(output_format) or result.get("markdown") or ""
        if not isinstance(content, str) or not content.strip():
            return "Error: Firecrawl 没有返回网页正文。"

        result_url = str(result.get("url") or url)
        return "URL: %s\nContent: %s" % (
            result_url,
            _truncate(content, self._content_limit()),
        )

    # ------------------------------------------------------------------
    # /firecrawl command
    # ------------------------------------------------------------------
    @filter.command("firecrawl")
    async def firecrawl_command(self, event: AstrMessageEvent):
        """查看自建 Firecrawl 状态，或手动执行搜索与网页读取。

        用法：/firecrawl status
              /firecrawl search 关键词
              /firecrawl read https://example.com
        """
        if not _as_bool(self._cfg("command_enabled", True), True):
            yield event.plain_result("Firecrawl 命令已在插件配置中禁用。")
            return

        args = _split_command_args(event.message_str)
        action = args[0].lower() if args else "status"
        rest = " ".join(args[1:]).strip()

        if action in ("status", "st", "状态"):
            yield event.plain_result(await self._status_text())
            return
        if action in ("search", "s", "搜索"):
            if not rest:
                yield event.plain_result("用法：/firecrawl search 关键词")
                return
            yield event.plain_result(await self._search_text(rest))
            return
        if action in ("read", "scrape", "r", "读取"):
            if not rest:
                yield event.plain_result("用法：/firecrawl read https://example.com")
                return
            yield event.plain_result(await self._read_text(rest.split()[0]))
            return

        yield event.plain_result(
            "用法：/firecrawl status | /firecrawl search 关键词 | /firecrawl read URL"
        )

    async def _status_text(self) -> str:
        """Build the /firecrawl status reply."""
        lines = [
            "Firecrawl 自建实例",
            "地址: %s" % self.client.base_url,
            "鉴权: %s"
            % (
                "已配置 %d 个 Key" % len(self.client.api_keys)
                if self.client.api_keys
                else "未配置 Key（适合 USE_DB_AUTHENTICATION=false 的自建实例）"
            ),
            "API 版本: %s" % (self.client.resolved_version or self.client.api_version),
        ]
        started = time.perf_counter()
        try:
            info = await self.client.ping()
            elapsed = (time.perf_counter() - started) * 1000
            message = str(info.get("message") or info.get("status") or "OK")
            lines.append("连接: OK (%.0f ms) - %s" % (elapsed, message))
        except FirecrawlError as exc:
            lines.append("连接: 失败 - %s" % exc)
            lines.append(
                "提示: AstrBot 若运行在 Docker 中，127.0.0.1 指向容器自身，请改成宿主机内网地址。"
            )
        return _truncate("\n".join(lines), COMMAND_REPLY_LIMIT)

    async def _search_text(self, query: str) -> str:
        """Build the /firecrawl search reply."""
        try:
            hits = await self.client.search(
                query, limit=max(1, _as_int(self._cfg("search_limit", 5), 5))
            )
        except FirecrawlError as exc:
            return _truncate("搜索失败: %s" % exc, COMMAND_REPLY_LIMIT)
        if not hits:
            return (
                "没有搜索到结果。自建实例的搜索依赖 SearXNG，偶发空结果时可直接重试。"
            )

        lines = ['搜索 "%s" 得到 %d 条结果：' % (query, len(hits))]
        for index, hit in enumerate(hits, 1):
            lines.append("%d. %s\n   %s" % (index, hit.title, hit.url))
            if hit.snippet:
                snippet = hit.snippet[:120] + ("…" if len(hit.snippet) > 120 else "")
                lines.append("   %s" % snippet)
        return _truncate("\n".join(lines), COMMAND_REPLY_LIMIT)

    async def _read_text(self, url: str) -> str:
        """Build the /firecrawl read reply."""
        if not url.lower().startswith(("http://", "https://")):
            return "请输入以 http:// 或 https:// 开头的地址。"
        try:
            result = await self.client.scrape(url, formats=[self._default_format()])
        except FirecrawlError as exc:
            return _truncate("读取失败: %s" % exc, COMMAND_REPLY_LIMIT)

        content = result.get(self._default_format()) or result.get("markdown") or ""
        if not isinstance(content, str) or not content.strip():
            return "没有读取到正文。"
        return _truncate(content, 1000)

    async def terminate(self) -> None:
        """Release the HTTP session when the plugin is unloaded."""
        logger.debug("firecrawl plugin terminating")
        try:
            await self.client.close()
        except Exception:  # pragma: no cover - defensive shutdown
            logger.exception("failed to close firecrawl client session")
