FROM python:3.12

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source is bind-mounted at runtime (see docker-compose.yml).
# docker run --rm -v "$(pwd):/app" -w /app <image>

CMD ["python"]
