FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    GSC_URL_INSPECTOR_CACHE=/cache \
    GSC_URL_INSPECTOR_DB=/data/gsc-inspections.sqlite
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . && useradd --system --uid 10001 app \
    && mkdir -p /cache /data && chown app /cache /data
USER app
VOLUME ["/cache", "/data"]
WORKDIR /work
ENTRYPOINT ["gsc-url-inspector"]
CMD ["--help"]
