import requests
import json

response = requests.get('http://127.0.0.1:8000/api/dashboard')
data = response.json()
for asset in data['assets']:
    print(f"Asset: {asset['asset']}, Bias: {asset['current']['bias']}, Display: {asset['current']['display']['label']}")