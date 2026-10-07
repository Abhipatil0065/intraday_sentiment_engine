import sys
import json
import urllib.request

url = "http://127.0.0.1:8000/api/dashboard"
response = urllib.request.urlopen(url)
data = json.load(response)

status = data.get('status', {})
print('stale:', status.get('stale'))
print('stale_after_minutes:', status.get('stale_after_minutes'))
print('last_poll_ok:', status.get('last_poll_ok'))
print('analysis_mode:', status.get('analysis_mode'))
print('running:', status.get('running'))