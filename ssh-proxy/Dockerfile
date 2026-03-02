FROM python:3.11-slim

# Labels
LABEL maintainer="infra-assistant"
LABEL description="SSH Proxy for Infra Assistant"

# Create app directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app.py .

# Create volume mount points
RUN mkdir -p /keys

# Don't run as root
RUN useradd -r -s /bin/false appuser && \
    chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 5001

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5001/health || exit 1

# Run with gunicorn for production
CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "2", "--timeout", "120", "app:app"]
