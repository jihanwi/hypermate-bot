FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# sqlite3 CLI for DB backups over `fly ssh console` (see README)
RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system app && useradd --system --gid app --home-dir /app --no-create-home app

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY hypermate/ hypermate/
COPY scripts/ scripts/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh && mkdir -p /data && chown app:app /data

# The entrypoint starts as root only to chown the mounted volume, then runs CMD as the app user.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "hypermate.main"]
