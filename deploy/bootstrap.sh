#!/bin/bash
# EC2 user-data: brings a bare Ubuntu box up as the Clip Editor server.
# Runs once, as root, on first boot. Log: /var/log/cloud-init-output.log
#
# Deliberately does NOT configure the public site or any secret. Caddy is left
# on its default (harmless) config and the app listens only on 127.0.0.1, so
# there is no window where the app is reachable without a password. provision.sh
# uploads .env and writes the real Caddyfile over SSH once the box is up.
set -euxo pipefail

REPO="${REPO_URL:-https://github.com/ZeptorAI/clipping-engine-for-hitesh.git}"
BRANCH="${BRANCH:-main}"
APP_DIR=/opt/clipeditor
RUN_USER=ubuntu

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-venv python3-pip ffmpeg git curl debian-keyring \
                   debian-archive-keyring apt-transport-https

# ---------- app ----------
git clone --depth 1 -b "$BRANCH" "$REPO" "$APP_DIR" || (cd "$APP_DIR" && git pull)
cd "$APP_DIR"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

mkdir -p "$APP_DIR/jobs"
touch "$APP_DIR/.env"
chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

# ---------- service (localhost only) ----------
cat >/etc/systemd/system/clipeditor.service <<'UNIT'
[Unit]
Description=Clip Editor (reels + VO tightening)
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/clipeditor
Environment=PYTHONUNBUFFERED=1
# 1 worker: jobs run in background threads and coordinate through status.json,
# and ffmpeg already saturates a small box. Long timeout covers big uploads.
ExecStart=/opt/clipeditor/.venv/bin/gunicorn -w 1 --threads 8 -t 3600 \
          -b 127.0.0.1:5000 app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now clipeditor

# ---------- caddy (installed, not yet pointed at the app) ----------
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -y
apt-get install -y caddy

# ---------- housekeeping: delete job files older than 7 days ----------
cat >/etc/cron.daily/clipeditor-cleanup <<'CRON'
#!/bin/sh
find /opt/clipeditor/jobs -mindepth 1 -maxdepth 1 -type d -mtime +7 \
  -exec rm -rf {} + 2>/dev/null || true
CRON
chmod +x /etc/cron.daily/clipeditor-cleanup

touch /var/lib/clipeditor-bootstrap-done
echo "BOOTSTRAP COMPLETE"
