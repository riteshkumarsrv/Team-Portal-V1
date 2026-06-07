FROM python:3.12-slim

# Nokia audit screenshot OCR (Tesseract; worksheet compare)
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV FLASK_RUN_HOST=0.0.0.0
ENV FLASK_RUN_PORT=5000
ENV FLASK_DEBUG=0
ENV TEAM_TRACKER_DB_PATH=/app/data/team_tracker.db

COPY requirements.txt requirements-deploy.txt .
RUN pip install --no-cache-dir -r requirements-deploy.txt

COPY app.py wsgi.py .
COPY static ./static
COPY templates ./templates

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "wsgi:app"]
