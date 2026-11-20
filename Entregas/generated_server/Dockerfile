FROM python:3.12-slim AS base

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY openapi.yaml .

ENV PYTHONPATH=/app/src
EXPOSE 8081

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8081/health', timeout=2)"

CMD ["uvicorn", "entregas_api.main:app", "--host", "0.0.0.0", "--port", "8081"]

FROM base AS test

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY tests ./tests
CMD ["pytest", "-q"]
