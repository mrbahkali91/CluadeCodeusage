FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -e .

# Least privilege: never run the application as root.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "sreoi_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
