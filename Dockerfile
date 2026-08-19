FROM python:3.12.1-slim-bookworm

WORKDIR /app

# FFmpeg — recording/transcoding ke liye required
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# pip update
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application files
COPY . .

ENV PORT=8000

EXPOSE 8000

# Production server
CMD ["sh", "-c", "gunicorn main:flask_app --bind 0.0.0.0:$PORT --workers 1 --worker-class gthread --threads 32 --timeout 120"]
