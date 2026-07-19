#!/bin/bash
# DEL installer — idempotent. Run as bjkai (uses sudo for system steps).
set -euo pipefail
cd /apps/del

echo "== 1. venv check"
test -x .venv/bin/uvicorn || { echo "venv missing/incomplete"; exit 1; }

echo "== 2. DB migrate + dirs"
mkdir -p database logs backups manifests
./scripts/del-admin migrate

echo "== 3. Install systemd units"
sudo cp config/del-helper.service /etc/systemd/system/del-helper.service
sudo cp config/del-web.service /etc/systemd/system/del-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now del-helper.service
sudo systemctl enable --now del-web.service

echo "== 4. Wait for local health"
for i in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8075/healthz >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS http://127.0.0.1:8075/healthz

echo "== 5. Nginx site"
TS=$(date +%Y%m%d-%H%M%S)
for f in /etc/nginx/sites-available/del.bjk.ai /etc/nginx/sites-enabled/del.bjk.ai; do
  [ -e "$f" ] && sudo cp -a "$f" "$f.bak.$TS"
done
sudo cp config/nginx-del.bjk.ai.conf /etc/nginx/sites-available/del.bjk.ai
sudo ln -sf /etc/nginx/sites-available/del.bjk.ai /etc/nginx/sites-enabled/del.bjk.ai
sudo nginx -t
sudo systemctl reload nginx

echo "== 6. HTTPS check"
curl -fsSI https://del.bjk.ai/login | head -1

echo "DONE. Create admin with: /apps/del/scripts/del-admin create-admin"
