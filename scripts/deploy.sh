#!/usr/bin/env bash
# Run on the server after cloning; credentials and state are outside the checkout.
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$repo_dir" != /opt/codex-reset-radar ]]; then
  echo 'Clone this repository to /opt/codex-reset-radar before deploying.' >&2
  exit 1
fi
if [[ "$EUID" -ne 0 ]]; then
  echo 'Run with sudo or as root (systemd service installation).' >&2
  exit 1
fi
exec 9>/run/codex-reset-radar-deploy.lock
flock -n 9 || { echo 'Another deployment is in progress.' >&2; exit 1; }
cd "$repo_dir"
[[ -z "$(git status --porcelain)" ]] || { echo 'Server checkout contains changes; refusing to overwrite.' >&2; exit 1; }
[[ "$(git branch --show-current)" == main ]] || { echo 'Expected branch main.' >&2; exit 1; }
git pull --ff-only origin main
python3 -c 'import radar.app'
install -d -m 755 /etc/codex-reset-radar
if [[ ! -f /etc/codex-reset-radar/config.json ]]; then
  install -m 644 config.example.json /etc/codex-reset-radar/config.json
fi
if [[ ! -s /etc/codex-reset-radar/radar.env ]]; then
  echo 'Create /etc/codex-reset-radar/radar.env with FEISHU_WEBHOOK_URL first.' >&2
  exit 1
fi
install -m 644 deploy/codex-reset-radar.service /etc/systemd/system/codex-reset-radar.service
systemctl daemon-reload
systemctl enable codex-reset-radar.service
systemctl restart codex-reset-radar.service
sleep 2
systemctl is-active --quiet codex-reset-radar.service
echo "Deployed $(git rev-parse --short HEAD)"
journalctl -u codex-reset-radar.service -n 12 --no-pager
