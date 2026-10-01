FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg: necessário para normalizar áudios Opus/WAV vindos do hardware BLE
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    libpq-dev \
    curl \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /code

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY app ./app
COPY templates ./templates

EXPOSE 8000

# A migração é executada no entrypoint do compose (api -> alembic upgrade head)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
