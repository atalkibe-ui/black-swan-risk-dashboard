#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$APP_ROOT"

node --check server/server.mjs
python3 -m py_compile research/build_*.py

python3 - <<'PY'
import json
from pathlib import Path

root = Path.cwd()
checks = {
    "gdelt-economic-narrative-history.json": ("records", 480),
    "economic-index-history.json": ("records", 1000),
    "deescalation-history.json": ("snapshots", 600),
    "political-index-history.json": ("snapshots", 600),
    "natural-biological-history.json": ("records", 900),
}
for name, (key, minimum) in checks.items():
    payload = json.loads((root / "research" / name).read_text(encoding="utf-8"))
    rows = payload.get(key)
    if not isinstance(rows, list) or len(rows) < minimum:
        raise SystemExit(f"{name}: массив {key} содержит меньше {minimum} наблюдений")

html = (root / "dashboard" / "black-swan-dashboard.html").read_text(encoding="utf-8")
required = (
    "ECONOMIC_INDEX_DATA_START",
    "DEESCALATION_DATA_START",
    "NATURAL_BIOLOGICAL_DATA_START",
    "Список потенциальных Black swan событий",
    "ai-analysis-file",
    "Документ Word (.docx)",
    "api/data-refresh",
    "data-refresh-label",
)
missing = [marker for marker in required if marker not in html]
if missing:
    raise SystemExit(f"HTML: отсутствуют маркеры {missing}")
print("Release validation passed")
PY
