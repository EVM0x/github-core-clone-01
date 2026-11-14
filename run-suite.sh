#!/usr/bin/env bash
# Restart the forge on a fresh store, then run the kit's conformance suite against it.
set -uo pipefail
KIT=/home/ubuntu/makefun/gh-kit/github-core-clone-01-kit
cd /home/ubuntu/gh-forge || exit 1
./build.sh || exit 1
pkill -f 'python3 main.py' 2>/dev/null
sleep 0.4
MAKEFUN_SHOP_TOKEN=tok PORT=8100 nohup python3 main.py > /tmp/forge.log 2>&1 &
for i in $(seq 1 40); do
  curl -sf http://127.0.0.1:8100/healthz >/dev/null 2>&1 && break
  sleep 0.1
done
cd "$KIT" || exit 1
node --experimental-strip-types sdk/run.ts --url http://127.0.0.1:8100 --token tok "$@"
