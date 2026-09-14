#!/bin/sh
set -eu
cd /opt/daodao-card-bot
.venv/bin/python -m scripts.backup_production
umask 077
stamp=$(date +%Y%m%d-%H%M%S)
# QQ state backup requires a quiet container for consistency.
docker compose stop napcat
trap 'docker compose start napcat' EXIT
tar -czf "backups/qq-session-$stamp.tar.gz" data/napcat data/qq .env
