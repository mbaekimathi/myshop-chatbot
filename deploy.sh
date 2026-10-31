#!/usr/bin/env bash
# Deploy on cPanel/Passenger — run after cloning or to update:
#
#   bash deploy.sh          (first time)
#   ./deploy.sh             (any time after)
#
# Steps: pull → install deps → migrate → collectstatic → restart.
# WhiteNoise serves staticfiles/ in production, so collectstatic MUST
# run after every pull or the site keeps serving stale JS/CSS.
set -euo pipefail
cd "$(dirname "$0")"

# Use the app's virtualenv if present (cPanel: ~/virtualenv/<APP>/<ver>/)
APP="$(basename "$PWD")"
for v in "$HOME"/virtualenv/"$APP"/*/bin/activate; do
  [ -f "$v" ] && source "$v" && break
done

git pull --ff-only

if [ -f requirements.txt ]; then
  pip install -q -r requirements.txt
fi

python manage.py migrate --noinput
python manage.py collectstatic --noinput --clear
mkdir -p tmp && touch tmp/restart.txt
echo "Deployed at $(git rev-parse --short HEAD) — Passenger restarting."
