import asyncio
import time
from app.analysis.llm_client import chat_json, LLM_CALL_TIMEOUT_SECONDS
from app.config import settings

async def test_llm_different_tokens():
    # Test with different max_tokens
    for max_tokens in [256, 512, 1024, 2048, 3000]:
        print(f'\n=== Testing max_tokens={max_tokens} ===')
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

        start = time.time()
        try:
            result = await chat_json(system_prompt, user_prompt, max_tokens=max_tokens)
            elapsed = time.time() - start
            print(f'  SUCCESS (took {elapsed:.2f}s)')
        except Exception as e:
            elapsed = time.time() - start
            print(f'  FAILED after {elapsed:.2f}s - {type(e).__name__}: {e}')

asyncio.run(test_llm_different_tokens())