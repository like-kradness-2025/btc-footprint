#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="${BTC_LIVE_DATA_DIR:-$ROOT/data/live}"
OUT_PNG="$ROOT/artifacts/footprint_chart.png"
LOG="$ROOT/logs/footprint_once.log"

mkdir -p "$ROOT/artifacts" "$ROOT/logs"
exec > >(tee -a "$LOG") 2>&1

echo "[$(date -Is)] start: BTC footprint chart"
python3 "$ROOT/gen_footprint.py" \
  --data-dir "$DATA_DIR" \
  --out "$OUT_PNG" \
  --hours 3 \
  --target-minutes 15 \
  --price-bin 10
echo "[$(date -Is)] done: $OUT_PNG"
