"""Probe financialjuice.com to find a usable feed.

Financial Juice renders its headlines client-side, so the plain HTML has no
article links. This probe reports what IS available so we can pick the
cheapest reliable integration (RSS > JSON API > headless render).
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

import httpx

BASE = "https://www.financialjuice.com"

CANDIDATE_FEEDS = [
    "/feed", "/rss.xml", "/rss", "/feed.xml", "/feed/rss", "/atom.xml",
    "/news/feed", "/feeds/all", "/api/news", "/api/feed",
]

URL_RE = re.compile(r"https?://[A-Za-z0-9.\-]+/[A-Za-z0-9/_\-.%]*")
SCRIPT_RE = re.compile(r"""<script[^>]+src=["']([^"']+)["']""")


def main() -> None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    with httpx.Client(timeout=20, headers=headers, follow_redirects=True) as c:
        home = c.get(BASE)
        text = home.text
        print(f"status={home.status_code} bytes={len(home.content)} final={home.url}")

        scripts = SCRIPT_RE.findall(text)
        print(f"\nscript bundles: {len(scripts)}")
        api_hits: set[str] = set()
        for src in scripts[:14]:
            url = urljoin(BASE, src)
            if url.startswith("//"):
                url = "https:" + url
            try:
                js = c.get(url).text
            except Exception as exc:  # noqa: BLE001
                print(f"  ERR {src[:60]} {type(exc).__name__}")
                continue
            found = set(re.findall(r"""["'](/?[A-Za-z0-9_\-]*api[A-Za-z0-9/_\-\.]*)["']""", js))
            found |= set(re.findall(r"""["'](https?://[A-Za-z0-9.\-]*financialjuice[A-Za-z0-9/_\-\.]*)["']""", js))
            if found:
                print(f"\n  {src[-55:]}")
                for f in sorted(found)[:14]:
                    print("     ", f)
                api_hits |= found

        # Any headline-like content already in the served HTML?
        print("\nheadline markers in raw HTML:")
        for key in ("news-headline", "headline", "articleTitle", "story-title",
                    "pubDate", "published", "GetNews", "newsList"):
            print(f"   {key:16} -> {text.count(key)}")


if __name__ == "__main__":
    main()