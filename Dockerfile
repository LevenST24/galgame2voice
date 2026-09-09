# ==============================================================================
# Galgame2Voice Production Container Image
# ==============================================================================
FROM python:3.11-slim

# Install system dependencies (ffmpeg for audio transcoding, curl for healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and static assets
COPY galgame2voice/ ./galgame2voice/
COPY scripts/ ./scripts/
COPY .env.example ./

# Create runtime directories
RUN mkdir -p /app/data /app/audio /app/logs

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    GALGAME_PORT=8080

EXPOSE 8080

VOLUME ["/app/data", "/app/audio", "/app/logs"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://127.0.0.1:8080/api/health || exit 1

# Launch uvicorn directly in Docker container
CMD ["uvicorn", "galgame2voice.main:app", "--host", "0.0.0.0", "--port", "8080"]
