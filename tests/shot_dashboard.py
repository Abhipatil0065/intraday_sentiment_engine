"""Render the dashboard headlessly and screenshot it, to verify layout.

Run:  python -m tests.shot_dashboard
"""

from __future__ import annotations


def main() -> None:
    from playwright.sync_api import sync_playwright

    from app.config import settings

    sizes = [(1920, 1080, "desktop"), (1366, 768, "laptop"), (1280, 600, "short")]
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        for w, h, label in sizes:
            ctx = b.new_context(user_agent=settings.http_user_agent,
                                viewport={"width": w, "height": h})
            p = ctx.new_page()
            errors: list[str] = []
            p.on("pageerror", lambda e: errors.append(str(e)))
            p.on("console",
                 lambda m: errors.append(f"{m.type}: {m.text}")
                 if m.type == "error" else None)
            p.goto("http://127.0.0.1:8000/", wait_until="networkidle",
                   timeout=30000)
            p.wait_for_timeout(2500)
            out = f"dashboard_{label}.png"
            p.screenshot(path=out, full_page=True)

            print(f"{label:8} {w}x{h}  assets={p.locator('.asset').count()} "
                  f"scopes={p.locator('.scope').count()} "
                  f"bars={p.locator('.bar').count()} "
                  f"feed={p.locator('.item').count()} "
                  f"drivers={p.locator('.note.drv').count()} "
                  f"risks={p.locator('.note.rsk').count()} "
                  f"catalysts={p.locator('.note.cat').count()}  -> {out}")
            if errors:
                print(f"         JS ERRORS: {errors[:5]}")
            ctx.close()
        b.close()


if __name__ == "__main__":
    main()