"""Dump the RENDERED Financial Juice page so we can see what actually loads.

Run:  python -m tests.probe_fj_render
"""

from __future__ import annotations

import re


def main() -> None:
    from playwright.sync_api import sync_playwright

    from app.config import settings

    with sync_playwright() as pw:
        b = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = b.new_context(
            user_agent=settings.http_user_agent,
            viewport={"width": 1440, "height": 3000},
            locale="en-US",
        )
        p = ctx.new_page()
        api_calls: list[str] = []
        p.on("request", lambda r: api_calls.append(r.url)
             if any(k in r.url.lower() for k in ("api", "json", "news", "feed"))
             else None)

        p.goto("https://www.financialjuice.com/", timeout=60000,
               wait_until="domcontentloaded")
        try:
            p.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        p.wait_for_timeout(5000)

        html = p.content()
        title = p.title()
        n_anchors = p.locator("a").count()
        body_text = p.inner_text("body")[:1200]

        b.close()

    print(f"title      : {title}")
    print(f"html bytes : {len(html)}")
    print(f"anchors    : {n_anchors}")
    print()
    print("=== XHR/API calls observed ===")
    for u in dict.fromkeys(api_calls):
        print("  ", u[:150])
    print()
    print("=== visible text (first 1200 chars) ===")
    print(body_text)
    print()
    print("=== markers ===")
    for k in ("Just a moment", "cf-browser-verification", "challenge",
              "Enable JavaScript", "news-headline", "headline"):
        print(f"  {k:28} -> {html.count(k)}")

    # ---- second pass: find the real news DOM and the FJService endpoints ----
    with sync_playwright() as pw2:
        b2 = pw2.chromium.launch(
            headless=True, args=["--no-sandbox",
                                 "--disable-blink-features=AutomationControlled"])
        ctx2 = b2.new_context(
            user_agent=settings.http_user_agent,
            viewport={"width": 1440, "height": 3000}, locale="en-US")
        p2 = ctx2.new_page()
        fj_calls: list[str] = []
        p2.on("request", lambda r: fj_calls.append(r.url)
              if "FJService" in r.url else None)
        p2.goto("https://www.financialjuice.com/", timeout=60000,
                wait_until="domcontentloaded")
        try:
            p2.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        p2.wait_for_timeout(6000)
        # Scroll to trigger any lazy rendering of the news list.
        p2.mouse.wheel(0, 6000)
        p2.wait_for_timeout(3000)

        print("\n=== FJService calls ===")
        for u in dict.fromkeys(fj_calls):
            print("  ", u[:190])

        n = p2.locator("[class*='headline']").count()
        print(f"\n=== headline elements: {n} ===")
        for i in range(min(4, n)):
            el = p2.locator("[class*='headline']").nth(i)
            try:
                print(f"\n--- [{i}] class = {el.get_attribute('class')!r}")
                print(f"    text = {el.inner_text().strip()[:220]!r}")
                print(f"    html = {el.evaluate('e => e.outerHTML')[:420]!r}")
            except Exception as exc:  # noqa: BLE001
                print(f"    err {exc}")
        b2.close()


if __name__ == "__main__":
    main()