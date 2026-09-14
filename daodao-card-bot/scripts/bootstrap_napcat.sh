#!/bin/sh
set -eu
cd /opt/daodao-card-bot
digest=sha256:406611383c31cc102665207b13cf0a4c2b463e27e300ba6ee5e7cb29adabd93f
image=docker.1ms.run/mlikiowa/napcat-docker@$digest
# Docker verifies the official manifest digest and every referenced layer.
docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image"
sh scripts/finish_napcat_install.sh
