FROM python:3.12-slim-bookworm
ARG VERSION=dev

# Unbuffered, so Docker captures log lines as they happen rather than in
# batches - a stalled collector should look stalled.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/London \
    APP_VERSION=$VERSION \
    HOME=/tmp

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector/ ./collector/

# Non-root. On my server, compose runs it as PUID 1003 / PGID 1006 (my app standard);
# this default keeps the image non-root when run without compose.
USER 1003:1006

EXPOSE 9109
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9109/healthz',timeout=4).status==200 else 1)"

CMD ["python", "-m", "collector.main"]
