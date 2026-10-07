# LLM CONTRADICTION ANALYSIS

## Current Configuration
- **Provider**: NVIDIA (https://integrate.api.nvidia.com/v1)
- **Model**: nvidia/nemotron-3-ultra-550b-a55b
- **Timeout**: LLM_CALL_TIMEOUT_SECONDS = 10s (hardcoded in llm_client.py:32)
- **Max tokens**: 3000 (settings.llm_max_tokens)
- **Concurrency**: 4 (settings.llm_max_concurrent)
- **Rate limit**: 40 req/min (settings.llm_requests_per_minute)

## Test Results

### 1. --check-llm Exact Error
```
RESULT: FAILED - provider returned 503 (overloaded) - falling back to heuristics
```

### 2. Real Article Classification Prompt Latency
| max_tokens | Result | Latency |
|------------|--------|---------|
| 256 (health check) | 503 overloaded | ~0.3-1.5s |
| 512 | 503 overloaded | ~0.3s |
| 1024 | 503 overloaded | ~0.3s |
| 2048 | 503 overloaded (circuit breaker) | ~0s |
| 3000 | 503 overloaded (circuit breaker) | ~0s |

**Note**: The health check previously succeeded in 1.58s with max_tokens=256, but now also returns 503. The provider appears to be consistently overloaded.

### 3. Concurrency Test (1 vs 4)
- **Concurrency 1**: First 3 calls get 503, circuit breaker opens, subsequent calls fail fast
- **Concurrency 4**: All calls fail immediately because circuit breaker opened from previous test

**503 errors appear regardless of concurrency** - they are provider-side overload, not rate limiting (429).

### 4. Pipeline Settings Confirmation
Both `--check-llm` and the pipeline use the same settings:
- `settings.llm_base_url`: https://integrate.api.nvidia.com/v1
- `settings.llm_model`: nvidia/nemotron-3-ultra-550b-a55b
- `settings.llm_timeout_seconds`: 20 (HTTP client timeout)
- `settings.llm_max_tokens`: 3000
- `LLM_CALL_TIMEOUT_SECONDS`: 10 (per-call asyncio.wait_for timeout)

## Root Cause Analysis

The Nemotron-3-Ultra model is a large reasoning model (550B parameters) that:
1. Takes longer to generate responses, especially with high token budgets
2. The 10s per-call timeout is too aggressive for 3000-token responses
3. The provider (NVIDIA NIM) is returning 503 (overloaded) consistently

The health check "works" only when the provider happens to respond quickly with a tiny 256-token response. Real classification requires 3000 tokens for the full schema.

## Proposed Fix (with Evidence)

### Option 1: Increase Timeout (Recommended)
Increase `LLM_CALL_TIMEOUT_SECONDS` from 10s to 30s in `app/analysis/llm_client.py:32`.

**Evidence**: Health check with 256 tokens takes ~1.5s. Scaling roughly linearly, 3000 tokens would need ~18s. A 30s timeout provides headroom.

### Option 2: Reduce Max Tokens
Reduce `settings.llm_max_tokens` from 3000 to 1024 in `.env` or config.

**Evidence**: The classification schema doesn't need 3000 tokens. 1024 should be sufficient for the JSON output.

### Option 3: Combine Both
- Increase timeout to 30s
- Reduce max_tokens to 1024
- This gives the best chance of success without changing provider/model

### Option 4: Reduce Concurrency
Reduce `settings.llm_max_concurrent` from 4 to 2 to reduce load on the provider.

**Evidence**: 503 is provider overload, not rate limit (429). Lower concurrency may help.

## Recommendation
Apply Option 3 (timeout 30s + max_tokens 1024) as the minimal fix. Monitor if 503 persists. If so, consider Option 4 or a lighter model (requires user approval).