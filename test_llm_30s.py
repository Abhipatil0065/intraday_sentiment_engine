import asyncio
import time
from app.analysis.llm_client import chat_json, LLM_CALL_TIMEOUT_SECONDS

async def test_llm_30s():
    print(f'Testing with timeout={LLM_CALL_TIMEOUT_SECONDS}s')
    
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
        result = await chat_json(system_prompt, user_prompt, max_tokens=1024)
        elapsed = time.time() - start
        print(f'chat_json: SUCCESS (took {elapsed:.2f}s)')
        print(f'Result keys: {list(result.keys())}')
    except Exception as e:
        elapsed = time.time() - start
        print(f'chat_json: FAILED after {elapsed:.2f}s - {type(e).__name__}: {e}')

asyncio.run(test_llm_30s())