"""WSGI entry for Gunicorn / production hosts: `gunicorn wsgi:app`."""

from app import create_app

app = create_app()
