#!/bin/sh
set -e
mkdir -p /app/data
if [ ! -f /app/data/team_tracker.db ]; then
  if [ -f "/app/Latest Database/team_tracker.db" ]; then
    cp "/app/Latest Database/team_tracker.db" /app/data/team_tracker.db
  fi
fi
exec gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 4 --timeout 120 wsgi:app
