FROM python:3.9-slim
WORKDIR /app

# Install dependencies
RUN apt-get update && apt-get install -y gcc
RUN apt-get install -y python3-dev libffi-dev

# Install requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app into the container
COPY main.py .
COPY .env .

# Unbuffer logs (helps with Docker logs in threads)
ENV PYTHONUNBUFFERED=1

# (Posterity only) Expose your port
EXPOSE 8080

# Run FastAPI
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
