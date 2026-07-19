#!/usr/bin/env bash
# make-demo-app.sh — create a DISPOSABLE demo app "deldemo" for DEL docs/screenshots
# and end-to-end removal validation. Additive-only; safe to run once.
# Companion teardown: make-demo-app.sh --down  (best-effort manual cleanup).
set -euo pipefail

APP=deldemo
ROOT=/apps/$APP
PORT=8076
DOMAIN=$APP.bjk.ai
CERT=/etc/letsencrypt/live/bjk.ai

teardown() {
  echo "== tearing down $APP (best-effort) =="
  (cd "$ROOT" 2>/dev/null && docker compose down -v --remove-orphans) || true
  docker volume rm ${APP}_data 2>/dev/null || true
  docker network rm ${APP}_net 2>/dev/null || true
  sudo systemctl disable --now ${APP}-heartbeat.timer 2>/dev/null || true
  sudo systemctl disable --now ${APP}-heartbeat.service 2>/dev/null || true
  sudo rm -f /etc/systemd/system/${APP}-heartbeat.service /etc/systemd/system/${APP}-heartbeat.timer
  sudo systemctl daemon-reload || true
  sudo rm -f /etc/cron.d/$APP
  sudo rm -f /etc/nginx/sites-enabled/$DOMAIN /etc/nginx/sites-available/$DOMAIN
  sudo nginx -t && sudo systemctl reload nginx || true
  rm -rf "$ROOT"
  echo "== teardown done =="
}

if [[ "${1:-}" == "--down" ]]; then teardown; exit 0; fi

echo "== creating disposable demo app: $APP =="

# free-port sanity check
if ss -lnt | grep -q "127.0.0.1:$PORT "; then
  echo "ERROR: port $PORT already in use" >&2; exit 1
fi

mkdir -p "$ROOT/logs" "$ROOT/html"

cat > "$ROOT/.env" <<EOF
DELDEMO_MODE=demo
EOF

cat > "$ROOT/html/index.html" <<EOF
<!doctype html><meta charset=utf-8><title>deldemo</title>
<h1>deldemo</h1><p>Disposable demo app for DEL discovery &amp; removal validation.</p>
EOF

cat > "$ROOT/compose.yaml" <<EOF
name: $APP
services:
  web:
    image: nginx:alpine
    container_name: ${APP}-web
    ports:
      - "127.0.0.1:$PORT:80"
    volumes:
      - ./html:/usr/share/nginx/html:ro
    networks:
      - ${APP}_net
    restart: unless-stopped
  worker:
    image: alpine:latest
    container_name: ${APP}-worker
    command: ["sh", "-c", "while true; do date >> /data/worker.log; sleep 3600; done"]
    env_file: .env
    volumes:
      - ${APP}_data:/data
      - ./logs:/logs
    networks:
      - ${APP}_net
    restart: unless-stopped
volumes:
  ${APP}_data:
networks:
  ${APP}_net:
EOF

# git init + commit
if [[ ! -d "$ROOT/.git" ]]; then
  git -C "$ROOT" init -q
  git -C "$ROOT" add -A
  git -C "$ROOT" -c user.email=demo@bjk.ai -c user.name=deldemo commit -qm "deldemo disposable demo app"
fi

# bring the compose project up
(cd "$ROOT" && docker compose up -d)

# nginx site
sudo tee /etc/nginx/sites-available/$DOMAIN >/dev/null <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name $DOMAIN;

    ssl_certificate $CERT/fullchain.pem;
    ssl_certificate_key $CERT/privkey.pem;

    client_max_body_size 50M;

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;
    add_header Strict-Transport-Security "max-age=31536000" always;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
    }
}
EOF
sudo ln -sfn /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/$DOMAIN
sudo nginx -t
sudo systemctl reload nginx

# systemd heartbeat service + timer (enable timer only)
sudo tee /etc/systemd/system/${APP}-heartbeat.service >/dev/null <<EOF
[Unit]
Description=DEL demo heartbeat (disposable, for DEL discovery/removal validation)

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'date >> $ROOT/logs/heartbeat.log'
WorkingDirectory=$ROOT
User=bjkai
EOF

sudo tee /etc/systemd/system/${APP}-heartbeat.timer >/dev/null <<EOF
[Unit]
Description=DEL demo heartbeat timer (disposable)

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ${APP}-heartbeat.timer

# cron entry
sudo tee /etc/cron.d/$APP >/dev/null <<EOF
17 3 * * * bjkai /bin/sh -c 'date >> $ROOT/logs/cron.log' # $APP marker
EOF

# DEL manifest (flat schema, matches del.yaml)
cat > /apps/del/manifests/$APP.yaml <<EOF
id: $APP
name: DEL Demo (disposable)
status: active
domains:
  - $DOMAIN
repositories:
  - $ROOT
host_paths:
  - $ROOT
compose:
  - $ROOT/compose.yaml
systemd_units:
  - ${APP}-heartbeat.service
  - ${APP}-heartbeat.timer
nginx:
  - /etc/nginx/sites-available/$DOMAIN
  - /etc/nginx/sites-enabled/$DOMAIN
cron:
  - /etc/cron.d/$APP
notes: |
  Disposable demo application created solely to exercise DEL's discovery and
  removal flow (screenshots + e2e validation). Safe to remove.
EOF

echo "== $APP created. Trigger a DEL scan to inventory it. =="
