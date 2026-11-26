FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    NAB_DATABASE=/data/empire_estory.db

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser && mkdir -p /data /app/static/uploads/empire
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt
COPY --chown=appuser:appuser . .
RUN chown -R appuser:appuser /data /app

USER appuser
EXPOSE 8000
CMD ["sh", "-c", "gunicorn --workers 2 --timeout 60 --bind 0.0.0.0:${PORT} wsgi:app"]
