# ==============================================================================
# GST Online Seller Integration - Production Dockerfile
# Multi-stage security-hardened container image for Python 3.12
# ==============================================================================

FROM python:3.12-slim AS runner

# System environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FLASK_CONFIG=production \
    PORT=5000

WORKDIR /app

# Install security updates & essential tools (curl for healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated non-root application user and group
RUN groupadd -r gstr --gid 10001 && \
    useradd -r -g gstr --uid 10001 --home-dir /app --shell /sbin/nologin gstr

# Install Python requirements
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt gunicorn

# Copy application source code
COPY . .

# Create and set permissions on data directories
RUN mkdir -p /app/instance /app/uploads /app/generated /app/exports /app/logs && \
    chown -R gstr:gstr /app

# Switch to non-root user
USER gstr

EXPOSE 5000

# Container liveness health check probe against /healthz
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT}/healthz || exit 1

# Start WSGI production server
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--threads", "2", "--timeout", "120", "wsgi:app"]
