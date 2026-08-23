FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Copy dependency file first (layer caching)
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy application code
COPY agent/ agent/
COPY cloud/ cloud/
COPY api/ api/

# Ensure data directory exists (watcher state persistence)
RUN mkdir -p /app/data

# Expose port
EXPOSE 8080

# Container-level health check against the API health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4); sys.exit(0 if r.status == 200 else 1)" || exit 1

# Long-lived server process:
# - single worker (agent/watcher state lives in-process)
# - trust proxy headers from the Cloud Run load balancer
# - keep-alive tuned for long-lived/streaming requests
# - graceful shutdown window for SIGTERM during scale-down/redeploys
CMD ["python", "-m", "uvicorn", "api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*", \
     "--timeout-keep-alive", "75", \
     "--timeout-graceful-shutdown", "30"]
