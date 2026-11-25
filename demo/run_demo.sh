#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python 3.10 or newer is required." >&2
  exit 1
}
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
echo "Open http://localhost:5000"
exec .venv/bin/python backend/main.py --dashboard
