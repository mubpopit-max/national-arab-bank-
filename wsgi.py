"""WSGI entry point for the Empire E-Story application.

Run with: gunicorn --workers 2 --bind 0.0.0.0:8000 wsgi:app
The legacy app module still owns authentication and its existing data store.
"""
from app import app
from empire_store import register_empire_store

register_empire_store(app)
