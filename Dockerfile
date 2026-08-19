FROM python:3.12.1-slim

# ffmpeg + ffprobe recording pipeline ke liye zaroori hai.
# build-essential — tgcrypto (Pyrogram ki fast MTProto crypto dependency)
# ke liye safety net hai agar iske pre-built wheel is exact platform ke
# liye available na ho to pip source se compile kar sake.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
EXPOSE 8000

# Flask ka built-in dev server (`python main.py`) production ke liye nahi
# bana — live HLS streaming me ek student ka browser hi ek time pe kai
# parallel requests (playlist + multiple segments) bhejta hai, aur dev
# server in concurrent long-lived connections ko reliably handle nahi kar
# pata — isi wajah se player "loading" pe hi atka reh jaata tha, especially
# jab ek se zyada students ek saath dekh rahe hon.
# Ab gunicorn (production WSGI server) — "gthread" worker class threads ke
# through concurrent I/O-bound requests (jaise ye proxy) ko sahi se serve
# karta hai. Shell form CMD taaki Render ka dynamic $PORT expand ho.
CMD gunicorn main:flask_app --bind 0.0.0.0:$PORT --workers 2 --worker-class gthread --threads 16 --timeout 120
