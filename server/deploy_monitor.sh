#!/bin/sh
set -eu

STAGE_DIR=${1:-/tmp/gmzz-dps-monitor-deploy}
SERVICE_USER=gmzz-dps-monitor
APP_DIR=/opt/gmzz-dps-monitor
DATA_DIR=/var/lib/gmzz-dps-monitor
ENV_FILE=/etc/gmzz-dps-monitor.env
UNIT_FILE=/etc/systemd/system/gmzz-dps-monitor.service
NGINX_FILE=/etc/nginx/conf.d/daodao-domain.conf

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

install -d -o root -g root -m 0755 "$APP_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$DATA_DIR"
install -o root -g root -m 0644 \
    "$STAGE_DIR/dps_monitor_server.py" "$APP_DIR/dps_monitor_server.py"
install -o root -g root -m 0644 \
    "$STAGE_DIR/gmzz-dps-monitor.service" "$UNIT_FILE"

if [ ! -f "$ENV_FILE" ]; then
    admin_password=$(openssl rand -hex 18)
    umask 077
    printf '%s\n' \
        'GMZZ_MONITOR_HOST=127.0.0.1' \
        'GMZZ_MONITOR_PORT=8766' \
        'GMZZ_MONITOR_DB=/var/lib/gmzz-dps-monitor/sessions.sqlite3' \
        'GMZZ_MONITOR_ADMIN_USER=admin' \
        'GMZZ_MONITOR_PARTNER_CARD=' \
        "GMZZ_MONITOR_ADMIN_PASSWORD=$admin_password" > "$ENV_FILE"
fi
chown root:root "$ENV_FILE"
chmod 0600 "$ENV_FILE"

systemctl daemon-reload
systemctl enable --now gmzz-dps-monitor.service
systemctl restart gmzz-dps-monitor.service

backup="$NGINX_FILE.backup.$(date +%Y%m%d%H%M%S)"
cp -a "$NGINX_FILE" "$backup"
install -o root -g root -m 0644 "$STAGE_DIR/daodao-domain.conf" "$NGINX_FILE"
if ! nginx -t; then
    cp -a "$backup" "$NGINX_FILE"
    nginx -t
    exit 1
fi
systemctl reload nginx.service

health_ok=0
for attempt in 1 2 3 4 5; do
    if curl --fail --silent --show-error \
        http://127.0.0.1:8766/api/v1/dps/health; then
        health_ok=1
        break
    fi
    sleep 1
done
if [ "$health_ok" -ne 1 ]; then
    systemctl status gmzz-dps-monitor.service --no-pager
    exit 1
fi
printf '\nDeployed GMZZ DPS monitor. Nginx backup: %s\n' "$backup"
