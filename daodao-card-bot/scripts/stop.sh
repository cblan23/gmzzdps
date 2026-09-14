#!/bin/sh
set -eu
systemctl stop daodao-card-bot daodao-card-api
cd /opt/daodao-card-bot
docker compose stop napcat
