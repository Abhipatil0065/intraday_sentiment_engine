"""
ONE-TIME CoinGlass login, so the scraper can read the "Legend" charts.

WHY THIS EXISTS
---------------
The Chart links shared by the user are COINGLASS "LEGEND" charts, and those are
NOT publicly readable: legend.coinglass.com/chart/<id> redirects anonymous
visitors to coinglass.com/login?act=legend (verified live). The BTC liquidation
heatmap page is public and needs no login, which is why BTC works out of the
box - but Gold and Silver live behind this login wall.

Logging into your own browser does NOT help: the scraper launches its own
separate Chromium, which shares no cookies with Chrome or Edge. This script
opens that browser, lets you sign in once, and saves the session into a profile
directory the scraper then reuses.

HOW TO USE IT
-------------
    python tools/coinglass_login.py

A browser window opens on CoinGlass's login page. Sign in with your
email/password (or the Google/Apple buttons). That is the ONLY step - the script
notices the login itself, saves the session, and closes the window.

Afterwards:

    # .env - uncomment this line
    COINGLASS_PROFILE_DIR=./data/coinglass_profile

    # then verify the metals now read
    python tools/verify_coinglass.py --asset XAGUSD

Re-run this script whenever the session expires.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

LOGIN_URL = "https://www.coinglass.com/login"
HOME_URL = "https://www.coinglass.com/"

#: Session cookies CoinGlass may set once signed in. Their presence is one of
#: several signals of a real session.
SESSION_COOKIE_NAMES = {
    "cg_session", "session", "token", "jwt", "auth_token", "cg_token",
    "uid", "user_id", "cg_user", "remember_web", "access_token",
    "refresh_token", "CG_SESSION", "auth", "user",
}

#: Local-storage keys that hold an auth token on some CoinGlass builds.
SESSION_STORAGE_KEYS = {
    "token", "access_token", "auth", "user", "uid", "cg_user", "session",
    "authorization", "jwt", "userInfo", "user_info", "cg_user_info",
}

#: URL paths that are ONLY reachable when signed in. Landing on one of these
#: IS proof of a session, whatever the cookies look like - CoinGlass redirects
#: anonymous visitors away from them.
AUTHENTICATED_PATHS = (
    "/account", "/user", "/portfolio", "/dashboard", "/settings",
    "/api-key", "/subscription", "/billing", "/profile",
)

#: Give up after this long, so a forgotten run cannot hang forever.
WAIT_SECONDS = 900


def _looks_signed_in(url: str, cookie_names: set,
                     storage_keys: set) -> tuple[bool, str]:
    """
    Decide whether the browser really carries an authenticated session.

    Returns (signed_in, why). Three independent signals, any of which counts:

      1. an authenticated URL - /login redirects to /account on success, and
         only a signed-in session survives there, so this is the strongest
         signal and needs no knowledge of CoinGlass's cookie names;
      2. a recognisable session cookie;
      3. a recognisable token in localStorage/sessionStorage.

    A token check alone is NOT sufficient: on the observed build the session
    survives in cookies whose names are not guessable, so a strict token test
    reports a successful login as a failure.
    """
    lowered = (url or "").lower()
    for path in AUTHENTICATED_PATHS:
        if path in lowered:
            return True, f"on an authenticated page ({url[:60]})"
    if cookie_names & SESSION_COOKIE_NAMES:
        return True, f"session cookie present: {sorted(cookie_names & SESSION_COOKIE_NAMES)[:3]}"
    if storage_keys & SESSION_STORAGE_KEYS:
        return True, f"token in storage: {sorted(storage_keys & SESSION_STORAGE_KEYS)[:3]}"
    return False, "no session cookie, token, or authenticated page yet"


def _profile_dir() -> Path:
    raw = (settings.coinglass_profile_dir or "").strip()
    path = Path(raw) if raw else Path("data/coinglass_profile")
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    path.mkdir(parents=True, exist_ok=True)
    return path


async def main() -> int:
    from playwright.async_api import async_playwright

    profile = _profile_dir()
    print("\n" + "=" * 70)
    print("COINGLASS LOGIN  (one time only)")
    print("=" * 70)
    print(f"  browser profile : {profile}")
    print(f"  login page       : {LOGIN_URL}")
    print("\nSTEP 1: a browser window is opening now.")
    print("STEP 2: sign in there with your CoinGlass email + password,")
    print("        or the Google / Apple buttons.")
    print("STEP 3: nothing else - this script notices you are signed in,")
    print("        saves the session and closes the window by itself.")
    print("\nWaiting for you to sign in...")

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(profile),
            headless=False,           # must be visible to log in by hand
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="UTC",
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(LOGIN_URL, timeout=60000,
                            wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            print(f"\ncould not open the login page: {exc}")
            await ctx.close()
            return 1

        signed_in = False
        reason = ""
        waited = 0
        try:
            while waited < WAIT_SECONDS:
                await page.wait_for_timeout(2000)
                waited += 2

                # Snapshot the browser's auth state.
                try:
                    names = {c.get("name") for c in await ctx.cookies()}
                except Exception:
                    names = set()
                try:
                    storage_keys = set(await page.evaluate(
                        "() => Object.keys(Object.assign({}, "
                        "localStorage || {}, sessionStorage || {}))"))
                except Exception:
                    storage_keys = set()
                try:
                    url = page.url
                except Exception:
                    url = ""

                ok, why = _looks_signed_in(url, names, storage_keys)
                if ok:
                    reason = why
                    await page.wait_for_timeout(3000)   # let the session settle
                    signed_in = True
                    break

                if waited % 30 == 0:
                    print(f"  ...still waiting ({waited}s). Sign in in the "
                          f"browser window. ({why})")
        except KeyboardInterrupt:
            # Ctrl+C must still SAVE whatever session exists, otherwise an
            # impatient interrupt throws away a perfectly good login.
            print("\n  interrupted - saving whatever session is already there.")
            try:
                names = {c.get("name") for c in await ctx.cookies()}
            except Exception:
                names = set()
            ok, why = _looks_signed_in(page.url, names, set())
            if ok:
                signed_in, reason = True, why

        if not signed_in:
            print(f"\nNO LOGIN DETECTED - nothing saved.")
            print("\nThe profile was created but holds no session, so the")
            print("scraper would only see the login wall. Check that you:")
            print("  * finished the login in the browser window that opened")
            print("  * were not blocked by a captcha or an email confirmation")
            print("  * did not close the window before signing in")
            await ctx.close()
            return 1
        print(f"\n  session detected: {reason}")

        # ---- END-TO-END VERIFICATION -----------------------------------
        # A detected session is not a working one: the credentials must also
        # survive a request to a page that is only reachable when signed in.
        # Without this check a lost session is reported as success, and the
        # failure only surfaces later as an unexplained redirect to /login.
        verify = await page.goto("https://www.coinglass.com/account",
                                 timeout=30000, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)
        final_url = page.url
        if "login" in final_url.lower():
            print("\nSESSION DID NOT PERSIST.")
            print("You did sign in, but the session was not saved, so the")
            print("scraper would still be sent to the login wall.")
            print("\nMost likely the window was closed before the session was")
            print("written. Run this script again and, this time, LEAVE THE")
            print("WINDOW OPEN until it prints LOGGED IN AND SAVED.")
            await ctx.close()
            return 1
        print(f"  verified: /account loads while signed in "
              f"(status {verify.status if verify else '?'})")

        # Keep the session warm: load the home page so the app registers the
        # cookie, then closing the persistent context flushes it to disk.
        try:
            await page.goto(HOME_URL, timeout=30000,
                            wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
        except Exception:  # noqa: BLE001 - not fatal, cookies are already set
            pass
        await ctx.close()

    # Report what was actually saved, so a "success" is verifiable rather than
    # assumed: the scraper can only read what is in here.
    try:
        names = {c.get("name") for c in await ctx.cookies()}
    except Exception:
        names = set()
    print("\n" + "=" * 70)
    print("LOGGED IN AND SAVED")
    print("=" * 70)
    print(f"\n  profile : {profile}")
    print(f"  session cookies saved: {sorted(names) or 'none'}")
    print("\nNext steps:")
    print("  1. uncomment this line in .env:")
    print("       COINGLASS_PROFILE_DIR=./data/coinglass_profile")
    print("  2. verify the metals now read:")
    print("       python tools/verify_coinglass.py --asset XAGUSD")
    print("       python tools/verify_coinglass.py --asset XAUUSD")
    print("\nNote: the session expires eventually. When Gold/Silver start")
    print("failing again, just re-run  python tools/coinglass_login.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))