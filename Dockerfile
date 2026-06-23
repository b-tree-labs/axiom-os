# ---- builder ----
FROM python:3.12-slim AS builder

WORKDIR /build

COPY pyproject.toml ./
COPY src/ src/

RUN pip install --no-cache-dir --prefix=/install \
    . \
    psycopg2-binary \
    requests \
    anthropic

# ---- runtime ----
FROM python:3.12-slim

LABEL org.opencontainers.image.version="0.3.1" \
      org.opencontainers.image.description="Modular digital platform for facilities"

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home neutron

COPY --from=builder /install /usr/local

WORKDIR /app

COPY runtime/ runtime/
COPY docs/ docs/

RUN chown -R 1000:1000 /app

USER 1000

EXPOSE 8766

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8766/health || exit 1

CMD ["python", "-m", "axiom.extensions.builtins.web_api.cli", "--port", "8766", "--host", "0.0.0.0"]
