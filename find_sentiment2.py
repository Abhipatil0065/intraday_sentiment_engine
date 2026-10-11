import os
import glob

# Find SentimentSnapshot creation
for root, dirs, files in os.walk(r'C:\Users\HP\.vscode\intraday_sentiment_engine\app'):
    for file in files:
        if file.endswith('.py'):
            filepath = os.path.join(root, file)
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                if 'SentimentSnapshot(' in content:
                    lines = content.split('\n')
                    for i, line in enumerate(lines):
                        if 'SentimentSnapshot(' in line:
                            print(f"{filepath}:{i+1}: {line.strip()}")