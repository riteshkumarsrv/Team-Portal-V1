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

COPY app.py wsgi.py main.py config.py .
COPY nokia_portal_roster.py sprint_hub_snapshot_png.py team_tracker_backup.py leave_grid_image.py .
COPY database ./database
COPY team_portal ./team_portal
COPY static ./static
COPY templates ./templates
COPY "Latest Database" ./Latest Database
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh && mkdir -p /app/data

EXPOSE 5000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
