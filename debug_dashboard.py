"""Check dashboard API response for confidence/bias probability values."""
import json
import urllib.request

resp = urllib.request.urlopen('http://127.0.0.1:8000/api/dashboard')
data = json.loads(resp.read())

for asset in data['assets']:
    print(f"\n=== {asset['asset']} ===")
    for scope_key in ('market_prep', 'current', 'market_wrap'):
        b = asset[scope_key]
        print(f"  {scope_key.upper()}:")
        print(f"    bias={b.get('bias')}")
        print(f"    score={b.get('score')}")
        print(f"    confidence={b.get('confidence')}")
        print(f"    raw_score={b.get('raw_score')}")
        print(f"    baseline_score={b.get('baseline_score')}")
        print(f"    contributing_articles={b.get('contributing_articles')}")
        print(f"    stale={b.get('stale')}")
