#!/bin/sh
set -eu
# Run as root from this project on Ubuntu 24.04. Does not restart DPS.
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
apt-get update -qq
apt-get install -y --no-install-recommends docker.io docker-compose-v2 python3-venv ca-certificates
systemctl enable --now docker
id daodao-card-bot >/dev/null 2>&1 || useradd --system --home /opt/daodao-card-bot --shell /usr/sbin/nologin daodao-card-bot
cd /opt/daodao-card-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock-linux.txt
.venv/bin/python -m scripts.initialize_deployment
chown -R daodao-card-bot:daodao-card-bot data logs
chown root:daodao-card-bot .env
chmod 640 .env
install -m 644 scripts/daodao-card-api.service /etc/systemd/system/
install -m 644 scripts/daodao-card-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now daodao-card-api daodao-card-bot
echo 'Services installed. Bot waits for QQ/group configuration.'
