# syntax=docker/dockerfile:1
# Intraday News Sentiment & Market Impact Engine - Docker
# Build:  docker compose build
# Run:    docker compose up -d        (restart: unless-stopped = 24/7)

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# System deps: lxml/beautifulsoup build tools, playwright/chromium deps (FinancialJuice calendar render), curl (healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libxml2-dev \
    libxslt1-dev \
    libffi-dev \
    curl \
    ca-certificates \
    tzdata \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Chromium for the FinancialJuice JS calendar renderer (optional; set FINANCIALJUICE_CALENDAR_ENABLED=false to skip)
RUN playwright install --with-deps chromium

# Application code (secrets / runtime data are NOT copied - injected at runtime)
COPY . /app

# Directories the app writes to (also mounted as persistent volumes in compose)
RUN mkdir -p /app/data /app/logs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD curl -f http://127.0.0.1:8000/api/health || exit 1

# 0.0.0.0 so the API is reachable from outside the container (docker port mapping)
CMD ["python", "run.py", "--api", "--host", "0.0.0.0", "--port", "8000"]
