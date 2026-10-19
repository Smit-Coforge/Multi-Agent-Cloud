FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY deploy/sample-input.json ./deploy/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
