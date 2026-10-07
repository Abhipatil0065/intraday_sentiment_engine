import sys
import json
import urllib.request

url = "http://127.0.0.1:8000/api/health"
response = urllib.request.urlopen(url)
d = json.load(response)

print('status:', d.get('status'))
print('last_poll_ok:', d.get('last_poll_ok'))
print('current_session:', d.get('current_session'))