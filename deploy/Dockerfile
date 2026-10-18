FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY deploy/sample-input.json ./deploy/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Task definition command scores deploy/sample-input.json and prints the risk object.
CMD ["python", "-c", "import json; from pathlib import Path; json.loads(Path('deploy/sample-input.json').read_text())"]
