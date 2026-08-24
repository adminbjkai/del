#!/bin/bash
# DEL installer — idempotent. Run as bjkai (uses sudo for system steps).
set -euo pipefail
cd /apps/del

echo "== 1. venv check"
test -x .venv/bin/uvicorn || { echo "venv missing/incomplete"; exit 1; }

echo "== 2. DB migrate + dirs"
mkdir -p database logs backups manifests
./scripts/del-admin migrate

echo "== 3. Install the privileged helper to a root-owned location"
# The helper runs as root. If its source and policy live in /apps/del (owned by
# the unprivileged web user) then a compromise of del-web can rewrite the code
# root is about to execute, and the operation allowlist becomes advisory. The
# repo stays the source of truth; these are the deployed, root-owned copies.
sudo install -d -o root -g root -m 0755 /usr/local/lib/del-helper
sudo install -o root -g root -m 0644 helper/del_helper.py  /usr/local/lib/del-helper/del_helper.py
sudo install -o root -g root -m 0644 helper/validation.py  /usr/local/lib/del-helper/validation.py
sudo install -d -o root -g root -m 0755 /etc/del
sudo install -o root -g root -m 0644 config/helper-policy.json /etc/del/helper-policy.json

echo "== 4. Install systemd units"
sudo cp config/del-helper.service /etc/systemd/system/del-helper.service
sudo cp config/del-web.service /etc/systemd/system/del-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now del-helper.service
sudo systemctl restart del-helper.service   # pick up newly installed helper code
sudo systemctl enable --now del-web.service

echo "== 4b. Restrict data files"
# The DB holds the admin password hash and session token hashes.
sudo chmod 0640 database/del.db 2>/dev/null || true
sudo chmod 0640 database/del.db-wal database/del.db-shm 2>/dev/null || true
chmod 0750 backups 2>/dev/null || true

echo "== 5. Wait for local health"
for i in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8075/healthz >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS http://127.0.0.1:8075/healthz

echo "== 6. Nginx site"
TS=$(date +%Y%m%d-%H%M%S)
for f in /etc/nginx/sites-available/del.bjk.ai /etc/nginx/sites-enabled/del.bjk.ai; do
  [ -e "$f" ] && sudo cp -a "$f" "$f.bak.$TS"
done
sudo cp config/nginx-del.bjk.ai.conf /etc/nginx/sites-available/del.bjk.ai
sudo ln -sf /etc/nginx/sites-available/del.bjk.ai /etc/nginx/sites-enabled/del.bjk.ai
sudo nginx -t
sudo systemctl reload nginx

echo "== 7. HTTPS check"
curl -fsSI https://del.bjk.ai/login | head -1

echo "DONE. Create admin with: /apps/del/scripts/del-admin create-admin"
