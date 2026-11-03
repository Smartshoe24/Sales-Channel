#!/usr/bin/env bash
# Sets up (or updates) Stepwhere Sales Desk on an Ubuntu server.
# Usage:  sudo bash /opt/sales-desk/deploy.sh sales.stepwhere.in
set -euo pipefail

DOMAIN="${1:-sales.stepwhere.in}"
REPO="https://github.com/Smartshoe24/Sales-Channel.git"
APP_DIR="/opt/sales-desk"
APP_USER="salesdesk"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this with sudo:  sudo bash $0 $DOMAIN"
  exit 1
fi

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git python3 curl gpg debian-keyring debian-archive-keyring apt-transport-https

if ! command -v caddy >/dev/null 2>&1; then
  echo "==> Installing Caddy (automatic HTTPS)"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

echo "==> Getting the app code"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO" "$APP_DIR"
fi
for f in hosted.py shim.js server.py index.html; do
  [ -f "$APP_DIR/$f" ] || { echo "Missing $f in the repository. Upload it to GitHub first, then run this again."; exit 1; }
done

echo "==> Creating the service user and folders"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR/data"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR/data"
chmod 700 "$APP_DIR/data"

if [ ! -f "$APP_DIR/.env" ]; then
  cat > "$APP_DIR/.env" <<'EOF'
# No spaces and no quotes around values.
SHOPIFY_STORE=
SHOPIFY_CLIENT_ID=
SHOPIFY_CLIENT_SECRET=
SHOPIFY_ACCESS_TOKEN=
SHOPIFY_API_VERSION=2026-07
SYNC_LOOKBACK_DAYS=30
EOF
  echo "==> Created a blank $APP_DIR/.env (fill it in, see the instructions)"
fi
chown "$APP_USER":"$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

echo "==> Installing the app service"
cat > /etc/systemd/system/sales-desk.service <<EOF
[Unit]
Description=Stepwhere Sales Desk
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=APP_HOST=$DOMAIN
Environment=PORT=8080
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 $APP_DIR/hosted.py
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$APP_DIR/data

[Install]
WantedBy=multi-user.target
EOF

echo "==> Setting up HTTPS for $DOMAIN"
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
    encode gzip
    header {
        Strict-Transport-Security "max-age=31536000"
        -Server
    }
    reverse_proxy 127.0.0.1:8080
}
EOF

systemctl daemon-reload
systemctl enable sales-desk >/dev/null 2>&1
systemctl restart sales-desk
systemctl enable caddy >/dev/null 2>&1
systemctl reload caddy 2>/dev/null || systemctl restart caddy

sleep 2
echo
echo "==> Status"
systemctl is-active sales-desk && echo "Sales Desk service: running"
systemctl is-active caddy && echo "Caddy (HTTPS): running"
echo
echo "Next steps:"
echo "  1. Add your first user:  sudo -u $APP_USER python3 $APP_DIR/hosted.py adduser YOURNAME"
echo "  2. Fill in Shopify:      sudo nano $APP_DIR/.env   then   sudo systemctl restart sales-desk"
echo "  3. Open https://$DOMAIN"
