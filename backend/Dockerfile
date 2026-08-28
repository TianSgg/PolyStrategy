FROM python:3.12-slim

ARG SERVICE_MODULE
ENV SERVICE_MODULE=${SERVICE_MODULE}

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vendor/ ./vendor/
RUN pip install --no-cache-dir -e ./vendor/py-clob-client-v2

COPY src/ ./src/

WORKDIR /app/src

CMD ["sh", "-c", "python -m ${SERVICE_MODULE}"]
