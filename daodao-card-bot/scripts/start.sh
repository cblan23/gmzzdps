#!/bin/sh
set -eu
cd /opt/daodao-card-bot
.venv/bin/python -m scripts.patch_napcat_private_route
docker compose up -d napcat
systemctl start daodao-card-api daodao-card-bot
