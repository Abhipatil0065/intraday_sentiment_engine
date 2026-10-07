import asyncio
import httpx
import time
from app.config import settings

async def test_llm():
    url = f"{settings.llm_base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "user", "content": "Say 'hello world' and nothing else."}
        ],
        "max_tokens": 10,
        "temperature": 0,
    }
    
    print(f"Testing {settings.llm_model} at {settings.llm_base_url}")
    print(f"Payload: {payload}")
    
    start = time.time()
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(url, headers=headers, json=payload)
            elapsed = time.time() - start
            print(f"HTTP Status: {resp.status_code}")
            print(f"Latency: {elapsed:.2f}s")
            print(f"Response: {resp.text}")
        except Exception as e:
            elapsed = time.time() - start
            print(f"Error after {elapsed:.2f}s: {type(e).__name__}: {e}")

asyncio.run(test_llm())