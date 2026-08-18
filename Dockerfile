FROM python:3.12.1-slim

# ffmpeg + ffprobe recording pipeline ke liye zaroori hai
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
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
#
# IMPORTANT: sirf 1 WORKER rakha hai (jyada threads se concurrency badhayi
# hai). Live-end auto-detection + download/upload background watcher
# threads process-memory (`recorder.py`'s in-memory `_active` dict) mein
# rehte hain — agar 2+ worker PROCESSES hote to har lecture ke liye har
# worker apna alag watcher start kar deta, jisse VIDEO DUPLICATE
# download+upload ho jaata. Single worker + zyada threads (gthread) se
# concurrency bhi poori milti hai aur ye duplication bhi nahi hota.
CMD gunicorn main:flask_app --bind 0.0.0.0:$PORT --workers 1 --worker-class gthread --threads 32 --timeout 120
