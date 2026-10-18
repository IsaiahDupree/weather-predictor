#!/bin/zsh
# Kalshi weather edge loop — autonomous capture + daily settlement/calibration.
#
# Invoked by cron. Each run captures the live Kalshi prod board fused with the
# free Open-Meteo ensemble + IEM/NWS obs (-> data/kalshi_snapshots/). Once a day
# (the SETTLE_HOUR_UTC run) it also settles every resolved event against the IEM
# ground-truth daily high and appends Brier + flagged-edge PnL to the
# calibration ledger (-> data/calibration/kalshi_calibration.jsonl).
#
# Read-only: it NEVER places an order. Manual run: bash scripts/kalshi_loop.sh
set -u

REPO="/Users/isaiahdupree/Documents/Software/WeatherPredictor"
PY="/opt/homebrew/bin/python3"
LOG="/tmp/kalshi-loop.log"
SETTLE_HOUR_UTC="14"   # ~07:00 ET / after the latest (Pacific) climate day closes + IEM ingest

export KALSHI_ENV="prod"
export WP_DEFAULT_VENUE="kalshi"
export WP_MODE="sim"
export WP_CITIES="nyc,chicago,la,miami,denver,austin,philadelphia"
export NWS_USER_AGENT="weatherpredictor research (isaiahdupree33@gmail.com)"
export IEM_USER_AGENT="weatherpredictor research (isaiahdupree33@gmail.com)"

cd "$REPO" || exit 1

# Self-lock (macOS has no flock): atomic mkdir; stale lock (>30m) is reclaimed.
LOCK="/tmp/kalshi-loop.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
    rmdir "$LOCK" 2>/dev/null; mkdir "$LOCK" 2>/dev/null || exit 0
  else
    exit 0   # another run is in progress
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
hour_utc="$(date -u +%H)"

echo "[$ts] capture ..." >> "$LOG"
"$PY" -m src.tools.kalshi_edge_snapshot --top 15 --min-depth 5 >> "$LOG" 2>&1

echo "[$ts] cross-venue basis scan (kalshi KNYC vs polymarket KLGA) ..." >> "$LOG"
"$PY" -m src.tools.cross_venue_basis --min-edge 0.05 --hist-days 365 >> "$LOG" 2>&1

if [ "$hour_utc" = "$SETTLE_HOUR_UTC" ]; then
  # Durable, easy-to-find verdict snapshot (data/ survives reboot; /tmp may not).
  RESULTS="$REPO/data/calibration/last_settle.txt"
  {
    echo "===== Kalshi weather settle @ $ts ====="
    echo "--- forecast/obs calibration (Brier + flagged-edge realized PnL) ---"
    "$PY" -m src.tools.kalshi_settle_calibrate --min-depth 5
    echo
    echo "--- cross-venue dislocation realized PnL (BASIS-RV vs DIRECTIONAL) ---"
    "$PY" -m src.tools.settle_cross_venue --min-depth 5
  } > "$RESULTS" 2>&1
  cat "$RESULTS" >> "$LOG"
fi
echo "[$ts] done." >> "$LOG"
