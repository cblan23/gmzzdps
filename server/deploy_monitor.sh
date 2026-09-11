#!/bin/sh
set -eu

STAGE_DIR=${1:-/tmp/gmzz-dps-monitor-deploy}
SERVICE_USER=gmzz-dps-monitor
APP_DIR=/opt/gmzz-dps-monitor
DATA_DIR=/var/lib/gmzz-dps-monitor
ENV_FILE=/etc/gmzz-dps-monitor.env
BUILD_ALLOWLIST_FILE=/etc/gmzz-dps-build-allowlist.json
RUNTIME_PROFILE_FILE=/etc/gmzz-dps-runtime-profile.json
CAPABILITY_PRIVATE_KEY_FILE=/etc/gmzz-dps-capability-signing-key.pem
UNIT_FILE=/etc/systemd/system/gmzz-dps-monitor.service
NGINX_FILE=/etc/nginx/conf.d/daodao-domain.conf

for required in \
    dps_monitor_server.py \
    profile_upload.py \
    runtime_capability.py \
    gmzz-dps-monitor.service \
    daodao-domain.conf \
    build-allowlist.json \
    runtime-profile.production.json
do
    if [ ! -f "$STAGE_DIR/$required" ]; then
        printf 'Missing deployment artifact: %s\n' "$STAGE_DIR/$required" >&2
        exit 1
    fi
done

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

install -d -o root -g root -m 0755 "$APP_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$DATA_DIR"
install -o root -g root -m 0644 \
    "$STAGE_DIR/dps_monitor_server.py" "$APP_DIR/dps_monitor_server.py"
install -o root -g root -m 0644 \
    "$STAGE_DIR/profile_upload.py" "$APP_DIR/profile_upload.py"
install -o root -g root -m 0644 \
    "$STAGE_DIR/runtime_capability.py" "$APP_DIR/runtime_capability.py"
install -o root -g root -m 0644 \
    "$STAGE_DIR/gmzz-dps-monitor.service" "$UNIT_FILE"
install -o root -g root -m 0644 \
    "$STAGE_DIR/build-allowlist.json" "$BUILD_ALLOWLIST_FILE"
install -o root -g "$SERVICE_USER" -m 0640 \
    "$STAGE_DIR/runtime-profile.production.json" "$RUNTIME_PROFILE_FILE"

capability_signing_key_id=$(
    python3 - "$STAGE_DIR/build-allowlist.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    value = json.load(handle)
key_ids = {
    str(build.get("capability_signing_key_id", "")).strip()
    for build in value.get("builds", [])
    if build.get("enabled") and build.get("protected")
}
key_ids.discard("")
if len(key_ids) > 1:
    raise SystemExit("deployment contains multiple protected capability key IDs")
print(next(iter(key_ids), ""))
PY
)
if [ -n "$capability_signing_key_id" ]; then
    if [ ! -f "$CAPABILITY_PRIVATE_KEY_FILE" ]; then
        printf 'Missing server-only capability key: %s\n' \
            "$CAPABILITY_PRIVATE_KEY_FILE" >&2
        exit 1
    fi
    python3 -c 'import cryptography' >/dev/null
    chown root:"$SERVICE_USER" "$CAPABILITY_PRIVATE_KEY_FILE"
    chmod 0640 "$CAPABILITY_PRIVATE_KEY_FILE"
fi

if [ ! -f "$ENV_FILE" ]; then
    admin_password=$(openssl rand -hex 18)
    umask 077
    printf '%s\n' \
        'GMZZ_MONITOR_HOST=127.0.0.1' \
        'GMZZ_MONITOR_PORT=8766' \
        'GMZZ_MONITOR_DB=/var/lib/gmzz-dps-monitor/sessions.sqlite3' \
        'GMZZ_PROFILE_HMAC_KEY_PATH=/var/lib/gmzz-dps-monitor/profile-character-hmac.key' \
        'GMZZ_PROFILE_SUPPORTED_BOSSES=' \
        'GMZZ_PROFILE_SUPPORTED_GAME_VERSIONS=' \
        'GMZZ_PROFILE_SENSITIVE_WORDS=' \
        'GMZZ_MONITOR_ADMIN_USER=admin' \
        'GMZZ_MONITOR_PARTNER_CARD=' \
        "GMZZ_MONITOR_BUILD_ALLOWLIST=$BUILD_ALLOWLIST_FILE" \
        'GMZZ_MONITOR_ENFORCE_BUILD_ALLOWLIST=1' \
        "GMZZ_MONITOR_RUNTIME_PROFILE=$RUNTIME_PROFILE_FILE" \
        "GMZZ_MONITOR_CAPABILITY_SIGNING_KEY_ID=$capability_signing_key_id" \
        "GMZZ_MONITOR_CAPABILITY_SIGNING_PRIVATE_KEY=$CAPABILITY_PRIVATE_KEY_FILE" \
        'GMZZ_MONITOR_RUNTIME_CAPABILITY_TTL=120' \
        "GMZZ_MONITOR_ADMIN_PASSWORD=$admin_password" > "$ENV_FILE"
fi

set_env_value() {
    key=$1
    value=$2
    if grep -q "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}
set_env_value GMZZ_MONITOR_BUILD_ALLOWLIST "$BUILD_ALLOWLIST_FILE"
set_env_value GMZZ_MONITOR_ENFORCE_BUILD_ALLOWLIST 1
set_env_value GMZZ_PROFILE_HMAC_KEY_PATH "$DATA_DIR/profile-character-hmac.key"
set_env_value GMZZ_MONITOR_RUNTIME_PROFILE "$RUNTIME_PROFILE_FILE"
set_env_value GMZZ_MONITOR_CAPABILITY_SIGNING_KEY_ID "$capability_signing_key_id"
set_env_value GMZZ_MONITOR_CAPABILITY_SIGNING_PRIVATE_KEY "$CAPABILITY_PRIVATE_KEY_FILE"
set_env_value GMZZ_MONITOR_RUNTIME_CAPABILITY_TTL 120
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
