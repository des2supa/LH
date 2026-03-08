#!/usr/bin/env bash
# One-shot setup: create venv, install deps, install Playwright browsers.
set -euo pipefail

VENV=".venv"

if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment…"
    python3 -m venv "$VENV"
fi

echo "Activating venv and installing dependencies…"
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -r requirements.txt -q

echo "Installing Playwright Chromium browser…"
"$VENV/bin/playwright" install chromium

echo ""
echo "Setup complete. Run the agent with:"
echo "  $VENV/bin/python main.py --origin FRA --destination LHR \\"
echo "      --outbound-date 2024-07-15 --outbound-time 10:00 \\"
echo "      --inbound-date  2024-07-22 --inbound-time  17:00 \\"
echo "      --cabin economy --currency EUR"
