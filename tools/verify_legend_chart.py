"""
Careful single-shot probe of the CoinGlass "Legend" chart URL.

The same chart ID was given for both gold and silver, which suggests this is a
MULTI-INSTRUMENT "Legend" chart holding several series rather than one
instrument per link. This loads it once, conservatively, and reports:
  - whether it renders or redirects to a login wall
  - what series/legends the page exposes
  - any price-axis labels and their magnitude (gold ~4k, silver ~50, btc ~80k)
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.ingestion.coinglass_scraper import _INSTRUMENT_JS  # noqa: E402

URL = "https://legend.coinglass.com/chart/ed5d16e5697a494fa6e4ab0f0586eaca"

INSPECT = r"""
() => {
  const out = {
    title: document.title,
    url: location.href,
    legend_labels: [],
    price_texts: [],
    canvas_count: document.querySelectorAll('canvas').length,
    svg_count: document.querySelectorAll('svg').length,
    body_len: document.body ? document.body.innerText.length : 0,
    body_head: document.body ? document.body.innerText.slice(0, 600) : ''
  };
  const texts = (window.__cgDrawLog && window.__cgDrawLog.texts) || [];
  texts.forEach(t => {
    const v = t.s.replace(/,/g, '');
    if (/^\d+(\.\d+)?$/.test(v) && parseFloat(v) > 10) {
      out.price_texts.push(t.s);
    }
  });
  // Series names the chart displays in its legend
  document.querySelectorAll('[class*="legend"] *, [class*="Legend"] *').forEach(el => {
    const t = (el.textContent || '').trim();
    if (t && t.length < 40 && el.children.length === 0) out.legend_labels.push(t);
  });
  out.legend_labels = [...new Set(out.legend_labels)].slice(0, 40);
  return out;
}
"""


async def main() -> int:
    from playwright.async_api import async_playwright
    print(f"loading: {URL}\n")
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        c = await b.new_context(
            user_agent=settings.coinglass_user_agent,
            viewport={"width": 1440, "height": 1600},
            locale="en-US")
        p = await c.new_page()
        await p.add_init_script(_INSTRUMENT_JS)
        try:
            await p.goto(URL, timeout=45000, wait_until="domcontentloaded")
        except Exception as exc:
            print(f"goto failed: {exc}")
            await b.close()
            return 1
        # Wait for the chart to draw, patiently but bounded.
        for _ in range(20):
            await p.wait_for_timeout(1000)
            try:
                n = await p.evaluate(
                    "() => (window.__cgDrawLog && window.__cgDrawLog.texts) ? "
                    "window.__cgDrawLog.texts.length : 0")
            except Exception:
                n = 0
            if n > 5:
                break
        info = await p.evaluate(INSPECT)
        await b.close()

    print(f"final url : {info['url']}")
    print(f"title     : {info['title']}")
    print(f"canvas={info['canvas_count']} svg={info['svg_count']} "
          f"text_len={info['body_len']}")
    print(f"legend    : {info['legend_labels']}")
    print(f"prices    : {info['price_texts'][:20]}")
    print(f"\nbody head : {info['body_head'][:400]}")

    if "login" in info["url"].lower():
        print("\nRESULT: redirected to a login wall - the chart is not readable "
              "without an authenticated session.")
        return 2
    if info["price_texts"]:
        print("\nRESULT: chart rendered with price-axis labels.")
        return 0
    print("\nRESULT: rendered, but no chart data captured.")
    return 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))