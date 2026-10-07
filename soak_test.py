"""
Soak test: run multiple pipeline ticks and collect timing/error stats.
"""
import asyncio
import time
from app.services.pipeline import run_once, runtime_state
from app.database import init_db

async def soak_test(num_ticks=3):
    init_db()
    durations = []
    errors_total = 0
    last_poll_ok_values = []
    
    print(f"Starting soak test with {num_ticks} ticks...")
    
    for i in range(num_ticks):
        print(f"\n=== Tick {i+1}/{num_ticks} ===")
        start = time.time()
        stats = await run_once()
        duration = time.time() - start
        durations.append(duration)
        errors_total += len(stats.errors)
        
        state = runtime_state()
        last_poll_ok = state.get("last_successful_tick")
        last_poll_ok_values.append(last_poll_ok)
        
        print(f"Duration: {duration:.1f}s")
        print(f"Fetched: {stats.fetched}, New: {stats.new_today}, Rejected: {stats.rejected}, Dup: {stats.duplicates}")
        print(f"LLM: ok={stats.llm_new_ok}, fail={stats.llm_new_failed}, skip_batch={stats.llm_new_skipped_batch}, skip_breaker={stats.llm_new_skipped_breaker}")
        print(f"Errors this tick: {len(stats.errors)}")
        print(f"Last poll OK: {last_poll_ok}")
        
        if stats.errors:
            for e in stats.errors[:3]:
                print(f"  ! {e}")
        
        # Wait between ticks (simulating poll interval)
        if i < num_ticks - 1:
            await asyncio.sleep(2)
    
    # Summary
    print("\n" + "="*60)
    print("SOAK TEST SUMMARY")
    print("="*60)
    print(f"Ticks completed: {len(durations)}")
    print(f"Min duration: {min(durations):.1f}s")
    print(f"Avg duration: {sum(durations)/len(durations):.1f}s")
    print(f"Max duration: {max(durations):.1f}s")
    print(f"Total errors: {errors_total}")
    print(f"Last poll OK advanced each tick: {all(last_poll_ok_values[i] != last_poll_ok_values[i-1] for i in range(1, len(last_poll_ok_values)))}")
    print(f"Last poll OK values: {last_poll_ok_values}")

if __name__ == "__main__":
    asyncio.run(soak_test(3))