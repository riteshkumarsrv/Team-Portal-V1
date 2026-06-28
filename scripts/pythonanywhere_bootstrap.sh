#!/bin/bash
# Run once in a PythonAnywhere Bash console (free tier).
# https://www.pythonanywhere.com → Consoles → Bash
set -euo pipefail

REPO="https://github.com/riteshkumarsrv/Team-Portal-V1.git"
PROJECT="$HOME/Team-Portal-V1"
VENV="team-portal-v1"
PY="python3.10"

echo "==> Clone or update Team Portal V1"
if [ -d "$PROJECT/.git" ]; then
  cd "$PROJECT"
  git pull origin main
else
  git clone "$REPO" "$PROJECT"
  cd "$PROJECT"
fi

echo "==> Virtualenv + dependencies"
if [ ! -d "$HOME/.virtualenvs/$VENV" ]; then
  mkvirtualenv --python="$PY" "$VENV"
fi
# shellcheck disable=SC1091
source "$HOME/.virtualenvs/$VENV/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Data directory"
mkdir -p "$PROJECT/data"
if [ ! -f "$PROJECT/data/team_tracker.db" ] && [ -f "$PROJECT/Latest Database/team_tracker.db" ]; then
  cp "$PROJECT/Latest Database/team_tracker.db" "$PROJECT/data/team_tracker.db"
fi

echo "==> Environment file"
if [ ! -f "$PROJECT/.env" ]; then
  cp "$PROJECT/.env.example" "$PROJECT/.env"
  SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
  sed -i "s|^FLASK_SECRET_KEY=.*|FLASK_SECRET_KEY=$SECRET|" "$PROJECT/.env"
  sed -i "s|^TEAM_TRACKER_PRODUCTION=.*|TEAM_TRACKER_PRODUCTION=1|" "$PROJECT/.env"
  sed -i "s|^# TEAM_TRACKER_PRODUCTION=1|TEAM_TRACKER_PRODUCTION=1|" "$PROJECT/.env"
  echo ""
  echo "IMPORTANT: edit $PROJECT/.env and set MANAGER_DASHBOARD_PASSWORD"
  echo "Also set PUBLIC_URL=https://$(whoami).pythonanywhere.com"
fi

echo "==> Smoke test import"
cd "$PROJECT"
python -c "from wsgi import app; print('WSGI OK:', app.name)"

echo ""
echo "Next (Web tab on PythonAnywhere):"
echo "  1. Add new web app → Manual configuration → Python $PY"
echo "  2. Virtualenv: $VENV"
echo "  3. Source code: $PROJECT"
echo "  4. WSGI: copy deploy/pythonanywhere_wsgi.py (replace YOUR_USERNAME with $(whoami))"
echo "  5. Static files: URL /static/ → $PROJECT/static/"
echo "  6. Reload web app"
echo "  7. Open https://$(whoami).pythonanywhere.com/login"
