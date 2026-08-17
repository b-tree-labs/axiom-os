FROM python:3.12-slim

LABEL org.opencontainers.image.title="axiom-sense-serve"
LABEL org.opencontainers.image.description="Consumer inbox ingestion server"

WORKDIR /app

COPY pyproject.toml .
COPY tools/ tools/

RUN pip install --no-cache-dir -e ".[sense,serve]"

EXPOSE 8765

ENTRYPOINT ["neut", "sense", "serve"]
CMD ["--host", "0.0.0.0", "--port", "8765"]
