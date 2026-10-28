#!/bin/bash
# Start FRIDAY. First run creates a virtualenv and installs dependencies.
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3)}"
if [ ! -d .venv ]; then
  echo "Creating virtualenv with $PY…"
  "$PY" -m venv .venv
fi
source .venv/bin/activate
if [ ! -f .venv/.deps ] || [ requirements.txt -nt .venv/.deps ]; then
  echo "Installing dependencies…"
  pip install -q --upgrade pip
  pip install -q -r requirements.txt
  touch .venv/.deps
fi
[ -f .env ] || cp .env.example .env
# Make sure the local brain is running (free, private).
if command -v ollama >/dev/null && ! curl -s -o /dev/null localhost:11434/api/version; then
  (ollama serve >/dev/null 2>&1 &) ; sleep 2
fi
exec python -m friday "$@"
