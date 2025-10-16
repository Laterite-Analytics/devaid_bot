# ── Stage 1: Build environment ─────────────────────────────
FROM python:3.11-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency files
COPY requirements.txt ./

# Install dependencies in a temporary build layer
RUN pip install --user --no-cache-dir -r requirements.txt


# ── Stage 2: Final lightweight runtime ─────────────────────
FROM python:3.11-slim

WORKDIR /app

# Copy installed site-packages from builder
COPY --from=builder /root/.local /root/.local

# Add the app code
COPY devaid.py ./devaid.py

# Ensure local installs are in path
ENV PATH=/root/.local/bin:$PATH

# Environment variables for runtime configuration
ENV PYTHONUNBUFFERED=1 \
    TZ=UTC

# Triggers the program every Tuesdays
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
