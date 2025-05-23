FROM python:3.9-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ffmpeg \
      gcc \
      ca-certificates \
      git \
      build-essential \
      libffi-dev \
      python3-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-keep-alive", "30"]
