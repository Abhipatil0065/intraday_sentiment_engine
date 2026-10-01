"""Raw provider probe - prints the unmodified API response for debugging.

Run:  python -m tests.probe_llm
Useful when a provider returns an unexpected shape (e.g. a reasoning model
that puts output in `reasoning_content` instead of `content`).
"""

from __future__ import annotations

import json

import httpx

from app.config import settings


def main() -> None:
    print(f"Base URL : {settings.llm_base_url}")
    print(f"Model    : {settings.llm_model}")
    print("-" * 70)

    import os

    variants = {
        "thinking-off": {"chat_template_kwargs": {"enable_thinking": False}},
        "plain": {},
    }
    for label, extra in variants.items():
        payload = {
            "model": settings.llm_model,
            "messages": [
                {"role": "system", "content": "Reply with JSON only."},
                {"role": "user", "content": 'Return exactly {"ok": true}'},
            ],
            "max_tokens": 64,
            "temperature": 0.0,
            **extra,
        }
        if settings.llm_json_mode:
            payload["response_format"] = {"type": "json_object"}

        r = httpx.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json=payload,
            timeout=120,
        )
        print(f"\n### {label} -> HTTP {r.status_code}")
        try:
            d = r.json()
            msg = (d.get("choices") or [{}])[0].get("message", {})
            usage = d.get("usage", {}).get("completion_tokens_details", {})
            print(f"  content          : {msg.get('content')!r}")
            print(f"  reasoning_tokens : {usage.get('reasoning_tokens')}")
            if not msg.get("content"):
                print("  RAW:", json.dumps(d, indent=1)[:1200])
        except Exception:  # noqa: BLE001
            print(r.text[:1200])


if __name__ == "__main__":
    import asyncio

    from app.analysis.llm_client import chat_json
    from app.analysis.news_classifier import SYSTEM_PROMPT

    headline = (
        "10-year Treasury yield hits highest level since 2002 as global "
        "bond rout gathers pace"
    )
    body = "Government bond yields rose sharply across developed markets today."

    async def go():
        raw = await chat_json(SYSTEM_PROMPT, f"HEADLINE:\n{headline}\n\nBODY:\n{body}")
        print("=" * 70)
        print("MODEL OUTPUT FOR A REAL HEADLINE")
        print("=" * 70)
        print(json.dumps(raw, indent=2)[:4000])

    asyncio.run(go())