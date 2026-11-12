FROM python:3.11-slim-bookworm
WORKDIR /app
COPY requirements.lock pyproject.toml README.md LICENSE ./
COPY posthub ./posthub
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home posthub && mkdir /data && chown posthub:posthub /data
USER posthub
ENV POSTHUB_DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3)"
ENTRYPOINT ["python", "-m", "posthub"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8787"]
