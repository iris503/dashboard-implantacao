# === Stage 1: build ===
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# === Stage 2: runtime ===
FROM python:3.12-slim

# Non-root user
RUN useradd -m -u 1000 appuser

# Copy installed packages
COPY --from=builder /root/.local /home/appuser/.local

WORKDIR /app
COPY --chown=appuser:appuser . .

# USER appuser  # running as root for DNS fix
ENV PATH=/home/appuser/.local/bin:$PATH

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health').read()" || exit 1

CMD ["sh", "-c", "echo nameserver 8.8.8.8 > /etc/resolv.conf && exec uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1"]
