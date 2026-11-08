#!/usr/bin/env bash
# Install EVE Tracker as a systemd service on a Raspberry Pi (Raspberry Pi OS / Debian).
# Run from the project directory:  ./deploy/install.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-$USER}"
cd "$DIR"

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10+ is required (Raspberry Pi OS Bookworm ships 3.11)." >&2
  exit 1
fi
if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
  echo "Installing python3-venv..."
  sudo apt-get update && sudo apt-get install -y python3-venv
fi

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env. Edit it with your EVE application's client ID/secret, then re-run this script."
fi

sed -e "s|__DIR__|$DIR|g" -e "s|__USER__|$USER_NAME|g" deploy/eve-tracker.service \
  | sudo tee /etc/systemd/system/eve-tracker.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable eve-tracker
sudo systemctl restart eve-tracker
sleep 2
systemctl --no-pager --lines=5 status eve-tracker || true
echo
echo "EVE Tracker is running at http://$(hostname).local:8000  (logs: journalctl -u eve-tracker -f)"
