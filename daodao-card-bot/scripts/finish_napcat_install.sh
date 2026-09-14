#!/bin/sh
set -eu
cd /opt/daodao-card-bot
digest=sha256:406611383c31cc102665207b13cf0a4c2b463e27e300ba6ee5e7cb29adabd93f
found=
for repo in docker.m.daocloud.io/mlikiowa/napcat-docker docker.1ms.run/mlikiowa/napcat-docker; do
  if docker image inspect "$repo@$digest" >/dev/null 2>&1; then
    docker tag "$repo@$digest" mlikiowa/napcat-docker:daodao-verified
    found=1
    break
  fi
done
if [ -z "$found" ]; then
  echo 'Verified image is not downloaded yet'
  exit 1
fi
.venv/bin/python -m scripts.activate_verified_image
.venv/bin/python -m scripts.patch_napcat_private_route
.venv/bin/python -m scripts.prepare_napcat
chown -R 1000:1000 data/napcat data/qq
docker compose up -d --pull never napcat
systemctl restart daodao-card-bot
echo 'NapCat started; scan with WebUI over SSH tunnel'
