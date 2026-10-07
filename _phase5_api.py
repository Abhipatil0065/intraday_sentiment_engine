"""Phase 5: API endpoint audit - valid, invalid, empty input."""
from fastapi.testclient import TestClient
from app.database import init_db
from app.main import app

# Use a fresh test database
init_db()

client = TestClient(app, raise_server_exceptions=False)

print("=" * 70)
print("PHASE 5: API ENDPOINT AUDIT")
print("=" * 70)

endpoints = [
    ("GET", "/api/health", None, "Liveness probe"),
    ("GET", "/api/dashboard", None, "Full dashboard payload"),
    ("GET", "/api/news", None, "News feed (first page)"),
    ("GET", "/api/news?limit=5&offset=0", None, "News feed (small page)"),
    ("GET", "/api/news?limit=0&offset=0", None, "News feed (limit=0)"),
    ("GET", "/api/news?limit=200&offset=0", None, "News feed (max page)"),
    ("GET", "/api/news?limit=10&offset=-1", None, "News feed (negative offset)"),
    ("GET", "/api/sentiment/XAUUSD", None, "Gold sentiment"),
    ("GET", "/api/sentiment/XAGUSD", None, "Silver sentiment"),
    ("GET", "/api/sentiment/BTCUSD", None, "Bitcoin sentiment"),
    ("GET", "/api/sentiment/xauusd", None, "Gold sentiment (lowercase)"),
    ("GET", "/api/sentiment/INVALID", None, "Unsupported asset"),
    ("GET", "/api/sentiment/", None, "Missing asset path param"),
    ("GET", "/api/alerts", None, "High-impact alerts"),
    ("GET", "/api/financialjuice/sessions", None, "FJ session analysis"),
    ("GET", "/api/console", None, "Plain-text console"),
    ("GET", "/", None, "HTML dashboard"),
    ("POST", "/api/run", None, "Trigger pipeline run"),
    ("GET", "/api/nonexistent", None, "Unknown endpoint"),
]

results = []
for method, path, body, desc in endpoints:
    try:
        if method == "GET":
            resp = client.get(path, follow_redirects=True)
        else:
            resp = client.post(path, json=body, follow_redirects=True)
        status = resp.status_code
        # Check for empty/error states
        try:
            data = resp.json()
            has_data = True
            data_summary = str(data)[:80]
        except Exception:
            has_data = False
            data_summary = resp.text[:80] if resp.text else "(empty)"

        # Check for secret leakage
        secret_leak = "nvapi" in str(resp.content) or "LLM_API_KEY" in str(resp.content)

        status_icon = "OK" if 200 <= status < 300 else "ERR" if status >= 400 else f"HTTP {status}"
        results.append((method, path, status, status_icon, has_data, secret_leak, desc))
        print(f"[{status_icon:>5}] {method:<4} {path:<45} status={status} data={'yes' if has_data else 'no'}  {desc}")
    except Exception as e:
        print(f"[EXC] {method:<4} {path:<45} {type(e).__name__}: {e}")
        results.append((method, path, 0, "EXC", False, False, desc))

print()
print("=" * 70)
print("VALIDATION SUMMARY")
print("=" * 70)

ok_count = sum(1 for r in results if r[2] and 200 <= r[2] < 300)
err_count = sum(1 for r in results if r[2] >= 400)
exc_count = sum(1 for r in results if r[3] == "EXC")
secret_count = sum(1 for r in results if r[5])

print(f"Success (2xx): {ok_count}")
print(f"Client/Server errors (4xx/5xx): {err_count}")
print(f"Exceptions: {exc_count}")
print(f"Secret leakage detected: {secret_count}")

# Check empty/error states
print()
print("--- Empty state checks ---")
# Dashboard with no data should return structure with stale=True blocks
resp = client.get("/api/dashboard")
try:
    dash = resp.json()
    first_asset = dash.get("assets", [{}])[0]
    mp = first_asset.get("market_prep", {})
    print(f"  Dashboard first asset market_prep:")
    print(f"    bias: {mp.get('bias', 'MISSING')}")
    print(f"    score: {mp.get('score', 'MISSING')}")
    print(f"    stale: {mp.get('stale', 'MISSING')}")
    print(f"    drivers: {mp.get('drivers', 'MISSING')}")
    print(f"    mode: {mp.get('mode', 'MISSING')}")
except:
    print("  Dashboard: could not parse JSON")

# Sentiment with unsupported asset
resp = client.get("/api/sentiment/INVALID")
print(f"\n  /api/sentiment/INVALID: status={resp.status_code}")
try:
    print(f"    body: {resp.json()}")
except:
    print(f"    body: {resp.text[:80]}")

# News with large offset
resp = client.get("/api/news?limit=10&offset=99999")
print(f"\n  /api/news?limit=10&offset=99999: status={resp.status_code}")
try:
    d = resp.json()
    print(f"    items: {len(d.get('items', []))}, total: {d.get('total', '?')}")
except:
    print(f"    body: {resp.text[:80]}")

# Console with no data
resp = client.get("/api/console")
has_content = len(resp.text) > 0
print(f"\n  /api/console: status={resp.status_code}, content={'yes' if has_content else 'no'}")
print(f"    first line: {resp.text.split(chr(10))[1] if len(resp.text.split(chr(10))) > 1 else resp.text[:60]}")

print()
print("=" * 70)
print("PHASE 5 COMPLETE")
print("=" * 70)
