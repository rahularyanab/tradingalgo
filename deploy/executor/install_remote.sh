#!/usr/bin/env bash
# Runs ON a subscriber droplet, as root, from the uploaded bundle (/opt/thetadesk-executor).
#   bash install_remote.sh <main-server-ip>
# Installs the executor in PAPER mode and locks the droplet down so only the Theta Desk main server
# can reach it (SSH). Safe to run again.
set -euo pipefail
MAIN_IP="$1"
CODE=/opt/thetadesk-executor
HOME_DIR=/opt/executor

timedatectl set-timezone Asia/Kolkata || true
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv ufw >/dev/null

python3 -m venv "$CODE/venv"
"$CODE/venv/bin/pip" install -q --upgrade pip
"$CODE/venv/bin/pip" install -q -r "$CODE/requirements.txt"

mkdir -p "$HOME_DIR"; chmod 700 "$HOME_DIR"
[ -f "$HOME_DIR/executor.env" ] || install -m 600 /dev/null "$HOME_DIR/executor.env"

# only the main server may connect (SSH); everything outgoing (broker, Theta Desk API) is allowed
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow from "$MAIN_IP" to any port 22 proto tcp >/dev/null
ufw --force enable >/dev/null

install -m 644 "$CODE/executor.service" /etc/systemd/system/thetadesk-executor.service
systemctl daemon-reload
systemctl enable thetadesk-executor.service >/dev/null
echo "INSTALLED"
