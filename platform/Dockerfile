FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

# libpq for psycopg, build-essential for anything without a wheel (argon2-cffi
# ships one, but a platform without it must still build).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Dependencies resolve from pyproject alone, so this layer is cached across
# source edits.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install -e .

# alembic.ini and migrations/ are needed at *runtime*, not build time: the
# entrypoint brings the schema up to head before serving.
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker/app-entrypoint.sh /usr/local/bin/app-entrypoint

# Least privilege: never run the application as root.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["app-entrypoint"]
CMD ["uvicorn", "sreoi_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
