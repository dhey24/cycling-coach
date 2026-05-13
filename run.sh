#!/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Prefer Homebrew Python 3.9 (matches existing venv); fall back to any python3
PYTHON3="/usr/local/opt/python@3.9/bin/python3.9"
if [ ! -x "$PYTHON3" ]; then
    PYTHON3="$(command -v python3)"
fi

if [ ! -d "venv" ]; then
    echo "[run.sh] Creating venv with $PYTHON3"
    "$PYTHON3" -m venv venv
    venv/bin/pip install -r requirements.txt
fi

echo "[run.sh] Starting $(date)"
exec venv/bin/python main.py "$@"
