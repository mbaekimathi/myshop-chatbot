#!/usr/bin/env bash
# Deploy on cPanel/Passenger: pull, refresh collected static, restart.
# WhiteNoise serves staticfiles/ in production, so collectstatic MUST
# run after every pull or the site keeps serving stale JS/CSS.
set -euo pipefail
cd "$(dirname "$0")"

# Use the app's virtualenv if present (cPanel: ~/virtualenv/<APP>/x.yz)
APP="$(basename "$PWD")"
for v in "$HOME"/virtualenv/"$APP"/*/bin/activate; do
  [ -f "$v" ] && source "$v" && break
done

git pull --ff-only
python manage.py collectstatic --noinput
mkdir -p tmp && touch tmp/restart.txt
echo "Deployed at $(git rev-parse --short HEAD) — Passenger restarting."
