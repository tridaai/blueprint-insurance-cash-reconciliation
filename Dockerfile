# Multi-stage build: dependencies are resolved in a builder stage, the
# runtime image carries only the package and a non-root user.
FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml constraints.txt README.md ./
COPY src ./src
RUN pip install --no-cache-dir -r constraints.txt \
 && pip install --no-cache-dir --no-deps .

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/cash-recon* /usr/local/bin/
COPY migrations ./migrations
# The production profile uses Postgres (compose sets DATABASE_URL).
# The image can also run the local SQLite profile: leave DATABASE_URL
# unset and the store lands in ./data (SQLITE_PATH moves it, e.g. to
# a mounted volume at /data/cashrecon.db).
RUN mkdir -p /app/data && chown appuser:appuser /app/data
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=10 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "cash_recon.api:app", "--host", "0.0.0.0", "--port", "8000"]
