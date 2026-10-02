#!/bin/sh
set -e

# Fly mounts the volume at /data owned by root. Give the DB directory to the app user
# (also fixes files root may have created via `fly ssh console`), then drop root.
if [ "$(id -u)" = "0" ]; then
    db_dir="$(dirname "${DATABASE_PATH:-/data/hypermate.db}")"
    mkdir -p "$db_dir"
    chown -R app:app "$db_dir"
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi

exec "$@"
