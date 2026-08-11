FROM python:3.11-slim-bookworm AS base

# Prevent Python from writing pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV WORKDIR=/app
WORKDIR ${WORKDIR}

# 1. Install OS-level shared libraries required by Scrapling (Playwright/Camoufox) & PostgreSQL client
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# 2. Upgrade pip and install project dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# 3. Download Scrapling browser binaries (Chromium + Camoufox)
RUN scrapling install

# 4. Copy application source code and entrypoint
COPY . .
RUN chmod +x docker/entrypoint.sh

# Expose FastAPI web service port
EXPOSE 8000

ENTRYPOINT ["docker/entrypoint.sh"]
