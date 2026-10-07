import requests
import json

resp = requests.get('http://127.0.0.1:8000/api/dashboard', timeout=5)
data = resp.json()

for asset in data['assets']:
    print(f"{asset['asset']}:")
    for scope in ['market_prep', 'market_wrap', 'current']:
        b = asset[scope]
        print(f"  {scope}: last_updated={b['last_updated']} bias={b['bias']} score={b['score']:.1f} conf={b['confidence']:.2f} n={b['contributing_articles']}")