import os
import subprocess

# Find SentimentSnapshot creation
result = subprocess.run(
    ['findstr', '/r', '/n', 'SentimentSnapshot(', 
     'C:\\Users\\HP\\.vscode\\intraday_sentiment_engine\\app\\*.py',
     'C:\\Users\\HP\\.vscode\\intraday_sentiment_engine\\app\\**\\*.py'],
    capture_output=True, text=True
)
print(result.stdout)