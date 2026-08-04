FROM python:3.12-slim

# Runs as a non-root user. The original ran as root from cron; there is no
# reason for a read-only data collector to have those privileges.
RUN useradd --uid 1009 --create-home --shell /usr/sbin/nologin wiser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector/ ./collector/

USER wiser

EXPOSE 9109

# Unbuffered, so Docker captures log lines as they happen rather than in
# batches - a stalled collector should look stalled.
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9109/healthz',timeout=4).status==200 else 1)"

CMD ["python", "-m", "collector.main"]
