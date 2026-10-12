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
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=10 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "cash_recon.api:app", "--host", "0.0.0.0", "--port", "8000"]
