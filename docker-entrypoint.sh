#!/bin/sh
set -eu

APP_DIR="${APP_DIR:-/usr/src/app}"
APP_USER="${APP_USER:-app}"
APP_GROUP="${APP_GROUP:-app}"
APP_UID="${PUID:-1000}"
APP_GID="${PGID:-1000}"
STATE_DB_PATH="${STATE_DB_PATH:-./data/state.sqlite3}"

case "$STATE_DB_PATH" in
    /*)
        STATE_DB_FILE="$STATE_DB_PATH"
        ;;
    *)
        STATE_DB_FILE="$APP_DIR/$STATE_DB_PATH"
        ;;
esac

STATE_DB_DIR="$(dirname "$STATE_DB_FILE")"

mkdir -p "$STATE_DB_DIR"

if ! getent group "$APP_GID" >/dev/null 2>&1; then
    groupadd -g "$APP_GID" "$APP_GROUP"
fi

if ! getent passwd "$APP_UID" >/dev/null 2>&1 && ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd -u "$APP_UID" -g "$APP_GID" -d "$APP_DIR" -M -s /usr/sbin/nologin "$APP_USER"
fi

chown -R "$APP_UID:$APP_GID" "$STATE_DB_DIR"
chmod 775 "$STATE_DB_DIR"

cd "$APP_DIR"
exec gosu "$APP_UID:$APP_GID" "$@"
