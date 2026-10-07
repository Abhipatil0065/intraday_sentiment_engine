"""
Test LLM concurrency - run classification prompts at different concurrency levels.
"""
import asyncio
import time
import os
from app.analysis.news_classifier import analyze_article
from app.config import settings

# Test article
TEST_TITLE = "Fed minutes show policymakers see another rate hike by year end"
TEST_BODY = "Federal Reserve officials signaled at their latest meeting that they see another interest rate hike by the end of the year as inflation remains above target. The minutes released Wednesday showed policymakers debated the need for further tightening."

async def run_concurrency_test(concurrency: int, num_calls: int = 10):
    """Run LLM classification at a specific concurrency level."""
    print(f"\n{'='*60}")
    print(f"Testing concurrency={concurrency}, calls={num_calls}")
    print(f"{'='*60}")
    
    # Temporarily override the semaphore
    from app.analysis.news_classifier import _LLM_SEMAPHORE, LLM_MAX_CONCURRENT
    original_semaphore = _LLM_SEMAPHORE
    import app.analysis.news_classifier as nc
    nc._LLM_SEMAPHORE = asyncio.Semaphore(concurrency)
    
    results = []
    start_time = time.time()
    
    async def make_call(i):
        call_start = time.time()
        try:
            analysis, llm_ok, skip_reason = await analyze_article(TEST_TITLE, TEST_BODY)
            call_end = time.time()
            return {
                "call": i,
                "success": llm_ok,
                "skip_reason": skip_reason,
                "latency": call_end - call_start,
                "error": None
            }
        except Exception as e:
            call_end = time.time()
            return {
                "call": i,
                "success": False,
                "skip_reason": "exception",
                "latency": call_end - call_start,
                "error": str(e)
            }
    
    # Run calls with limited concurrency
    semaphore = asyncio.Semaphore(concurrency)
    
    async def limited_call(i):
        async with semaphore:
            return await make_call(i)
    
    tasks = [limited_call(i) for i in range(num_calls)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Handle any exceptions
    processed_results = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            processed_results.append({
                "call": i,
                "success": False,
                "skip_reason": "exception",
                "latency": 0,
                "error": str(r)
            })
        else:
            processed_results.append(r)
    
    total_time = time.time() - start_time
    
    # Print results
    success_count = sum(1 for r in processed_results if r["success"])
    failed_count = sum(1 for r in processed_results if not r["success"])
    
    print(f"Total time: {total_time:.2f}s")
    print(f"Successful: {success_count}, Failed: {failed_count}")
    
    latencies = [r["latency"] for r in processed_results if r["success"]]
    if latencies:
        print(f"Latencies (successful): min={min(latencies):.2f}s, max={max(latencies):.2f}s, avg={sum(latencies)/len(latencies):.2f}s")
    
    for r in processed_results:
        status = "OK" if r["success"] else "FAIL"
        print(f"  Call {r['call']}: {status} - {r['skip_reason']} - {r['latency']:.2f}s", end="")
        if r["error"]:
            print(f" - {r['error'][:100]}")
        else:
            print()
    
    # Restore original semaphore
    nc._LLM_SEMAPHORE = original_semaphore
    
    return processed_results

async def main():
    print(f"LLM Config:")
    print(f"  Base URL: {settings.llm_base_url}")
    print(f"  Model: {settings.llm_model}")
    print(f"  Has LLM: {settings.has_llm}")
    print(f"  Max Concurrent: {settings.llm_max_concurrent}")
    print(f"  Requests/min: {settings.llm_requests_per_minute}")
    
    if not settings.has_llm:
        print("\nNo LLM configured - skipping tests")
        return
    
    # Reset circuit breaker
    from app.analysis import llm_client
    await llm_client.reset_circuit_breaker()
    
    # Test at different concurrency levels
    for concurrency in [1, 2, 4]:
        await run_concurrency_test(concurrency, 10)
        # Wait a bit between tests
        await asyncio.sleep(5)
    
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(main())