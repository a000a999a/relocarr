#!/bin/sh
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if ! getent group relocarr >/dev/null 2>&1; then
    groupadd -g "$PGID" relocarr
fi
if ! getent passwd relocarr >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -d /app -s /sbin/nologin relocarr
fi

mkdir -p /config
chown -R relocarr:relocarr /config

exec gosu relocarr "$@"
