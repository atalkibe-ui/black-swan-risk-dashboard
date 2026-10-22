#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DEFAULT_NAME="black-swan-dashboard-transfer-$(date -u +%Y%m%d).tar.gz"
OUTPUT="${1:-$PWD/$DEFAULT_NAME}"

if [[ "$OUTPUT" != /* ]]; then
  OUTPUT="$PWD/$OUTPUT"
fi

mkdir -p "$(dirname "$OUTPUT")"
"$APP_ROOT/deployment/validate-release.sh" "$APP_ROOT"

items=(dashboard deployment research server)
if [[ -f "$APP_ROOT/README.md" ]]; then
  items=(README.md "${items[@]}")
fi

tar \
  --exclude='.cache' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='server/data' \
  --exclude='server/prompts' \
  -czf "$OUTPUT" \
  -C "$APP_ROOT" \
  "${items[@]}"

checksum="$(sha256sum "$OUTPUT" | awk '{print $1}')"
printf '%s  %s\n' "$checksum" "$(basename "$OUTPUT")" > "$OUTPUT.sha256"
printf 'Created %s\nChecksum %s\n' "$OUTPUT" "$OUTPUT.sha256"
