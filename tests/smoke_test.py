"""Smoke test against a live self-hosted Firecrawl instance.

Usage:
    python tests/smoke_test.py http://127.0.0.1:3002

The script only needs aiohttp and prints the shape of every response, which
makes it useful when checking a fresh deployment before wiring it into chat.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from firecrawl_client import FirecrawlClient, FirecrawlConnectionError  # noqa: E402


async def check(base_url: str) -> int:
    """Run the checks and return a process exit code."""
    client = FirecrawlClient(base_url, timeout=90)
    failures = 0
    try:
        info = await client.ping()
        print("[ping]  ", info)

        hits = await client.search("firecrawl self host", limit=2)
        print("[search] %d hit(s)" % len(hits))
        for hit in hits:
            print("   - %s | %s | %s" % (hit.title[:50], hit.url, hit.snippet[:60]))
        if not hits:
            failures += 1

        data = await client.scrape("https://example.com")
        markdown = data.get("markdown") or ""
        print("[scrape] keys=%s markdown_chars=%d" % (sorted(data)[:6], len(markdown)))
        if not markdown:
            failures += 1

        print("[version] %s" % client.resolved_version)
    finally:
        await client.close()

    offline = FirecrawlClient("http://127.0.0.1:9", timeout=2)
    try:
        await offline.ping()
        print("[offline] unexpected success")
        failures += 1
    except FirecrawlConnectionError as exc:
        print("[offline] correctly reported: %s" % exc)
    finally:
        await offline.close()

    return 1 if failures else 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3002"
    raise SystemExit(asyncio.run(check(base)))
