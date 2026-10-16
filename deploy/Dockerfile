FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY deploy/run_risk.py deploy/sample-input.json ./deploy/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV OLLAMA_HOST=http://ollama.underwriting.internal:11434

CMD ["python", "deploy/run_risk.py"]
