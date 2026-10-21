#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON="${PYTHON:-$APP_ROOT/.venv/bin/python}"
CACHE_ROOT="${CACHE_ROOT:-/var/lib/black-swan-dashboard/source-cache}"
DASHBOARD="$APP_ROOT/dashboard/black-swan-dashboard.html"
RESEARCH="$APP_ROOT/research"
BLOOMBERG_WORKBOOK="${BLOOMBERG_WORKBOOK:-$RESEARCH/Blackswan.xlsx}"
REOK_WORKBOOK="${REOK_WORKBOOK:-$RESEARCH/REOK.xlsx}"

test -r "$BLOOMBERG_WORKBOOK"
test -r "$REOK_WORKBOOK"

mkdir -p "$CACHE_ROOT"
cd "$APP_ROOT"

"$PYTHON" "$RESEARCH/build_economic_narrative_stress.py" \
  --cache-dir "$CACHE_ROOT/economic-narrative" \
  --output "$RESEARCH/economic-narrative-stress.json"

"$PYTHON" "$RESEARCH/build_central_bank_communication_stress.py" \
  --cache-dir "$CACHE_ROOT/central-bank-communications" \
  --output "$RESEARCH/central-bank-communication-stress.json"

"$PYTHON" "$RESEARCH/build_economic_history.py" \
  --workbook "$BLOOMBERG_WORKBOOK" \
  --reok "$REOK_WORKBOOK" \
  --output "$RESEARCH/economic-index-history.json" \
  --backtest "$RESEARCH/economic-index-backtest.json" \
  --report "$RESEARCH/economic-index-backtest.md" \
  --html "$DASHBOARD"

"$PYTHON" "$RESEARCH/build_deescalation_history.py" \
  --cache-dir "$CACHE_ROOT/deescalation-google-news" \
  --output "$RESEARCH/deescalation-history.json" \
  --dashboard "$DASHBOARD"

"$PYTHON" "$RESEARCH/build_political_history.py" \
  --russia "$RESEARCH/deescalation-history.json" \
  --cache-dir "$CACHE_ROOT/political-google-news" \
  --output "$RESEARCH/political-index-history.json" \
  --dashboard "$DASHBOARD"

"$PYTHON" "$RESEARCH/build_natural_biological_history.py" \
  --cache-dir "$CACHE_ROOT/natural-biological" \
  --output "$RESEARCH/natural-biological-history.json" \
  --backtest "$RESEARCH/natural-biological-backtest.json" \
  --report "$RESEARCH/natural-biological-backtest.md" \
  --dashboard "$DASHBOARD"

"$APP_ROOT/deployment/validate-release.sh" "$APP_ROOT"
