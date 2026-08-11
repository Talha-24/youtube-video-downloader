# Base image with Python
FROM python:3.11-slim

# ffmpeg is required for merging video+audio and for MP3 extraction
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render sets $PORT at runtime; gunicorn is a production-grade server
# (Flask's built-in dev server, which app.py uses for local runs, isn't
# meant for production traffic).
CMD gunicorn -w 2 -b 0.0.0.0:$PORT --timeout 600 app:app
