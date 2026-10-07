import asyncio
import time
from app.analysis.llm_client import chat_json, _semaphore_fixed, _limiter_fixed
from app.config import settings

async def test_concurrency(concurrency):
    print(f'\n=== Testing concurrency={concurrency} ===')
    
    # Create new semaphore for this test
    sem = asyncio.Semaphore(concurrency)
    
    system_prompt = """You are a financial news classifier. Output ONLY valid JSON with this schema:
{
  "category": "string",
  "importance": "string",
  "report_kind": "string",
  "verification": "string",
  "relevance_score": 0.5,
  "mode": "llm",
  "note": "string",
  "assets": [
    {"asset": "GOLD", "direction": "BEARISH", "strength": "MODERATE", "relevance": 0.5, "persistence": 0.5, "reason": "string", "transmission": "string", "metal_character": "string"},
    {"asset": "SILVER", "direction": "BEARISH", "strength": "MODERATE", "relevance": 0.5, "persistence": 0.5, "reason": "string", "transmission": "string", "metal_character": "string"},
    {"asset": "BITCOIN", "direction": "BEARISH", "strength": "MODERATE", "relevance": 0.5, "persistence": 0.5, "reason": "string", "transmission": "string", "metal_character": "string"}
  ],
  "event_summary": "string"
}"""

    user_prompt = """Classify this financial news article:

Title: Gold down 1% as dollar gains; Fed minutes in focus
Body: Gold prices fell 1% on Monday as the U.S. dollar strengthened ahead of the Federal Reserve meeting minutes. The dollar index rose 0.3% making gold more expensive for overseas buyers. Traders are focused on the Fed minutes for clues on the path of interest rates. Spot gold was down 1% at $1,920 per ounce."""

    async def single_call(i):
        async with sem:
            start = time.time()
            try:
                result = await chat_json(system_prompt, user_prompt, max_tokens=3000)
                elapsed = time.time() - start
                return (i, True, elapsed, None)
            except Exception as e:
                elapsed = time.time() - start
                return (i, False, elapsed, str(e))
    
    tasks = [single_call(i) for i in range(4)]
    results = await asyncio.gather(*tasks)
    
    for i, success, elapsed, error in results:
        if success:
            print(f'  Call {i}: SUCCESS in {elapsed:.2f}s')
        else:
            print(f'  Call {i}: FAILED in {elapsed:.2f}s - {error}')

asyncio.run(test_concurrency(1))
asyncio.run(test_concurrency(4))