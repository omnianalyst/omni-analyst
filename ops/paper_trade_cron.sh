#!/usr/bin/env bash
# Daily paper-trader cron wrapper.
#
# Why this exists as a shell script, not Python:
#   - cron wants a single executable; this is that executable
#   - the host's cron runs this, NOT the deployed app's scheduler
#   - keeps the paper trader fully decoupled from the app deployment:
#     redeploys via teploy do not interrupt the daily collection
#
# What this does:
#   1. scan  - open new paper positions on markets the LLM disagrees with
#   2. resolve - close out positions whose markets have resolved
#   3. report - append a daily summary to the log
#
# Persistence: the JSONL log lives at $PAPER_TRADES_LOG, which defaults to
# /var/lib/omni-analyst/paper_trades.jsonl. THIS PATH MUST BE ON PERSISTENT
# STORAGE. If you point it inside the app's deploy directory, a teploy
# redeploy will wipe it. The default /var/lib/omni-analyst/ survives.
#
# Install (one-time, on the host that runs the trader — e.g. Proxmox box):
#
#   sudo mkdir -p /var/lib/omni-analyst
#   sudo chown $USER:$USER /var/lib/omni-analyst
#
#   # add to crontab (crontab -e):
#   17 6 * * *  /path/to/omni-analyst/app-v2/ops/paper_trade_cron.sh >> /var/log/paper_trader.log 2>&1
#
# That runs at 06:17 daily. The off-peak hour avoids Polymarket's
# peak-ish traffic and reduces the chance of rate-limit issues.
#
# Required env (set in /etc/environment, systemd unit, or a sourced file):
#   GLM_API_KEY       - your Zhipu/z.ai API key
#   PAPER_REPO_DIR    - absolute path to omni-analyst/app-v2 (for finding the python module)
#
# Optional env (with defaults):
#   PAPER_TRADES_LOG  - default /var/lib/omni-analyst/paper_trades.jsonl
#   PAPER_TARGET_MARKETS - default 50 (how many active markets to scan per day)
#   PAPER_THRESHOLD   - default 0.05 (min |llm_prob - market_price| to open)
#   PAPER_SIZE_USD    - default 5.0 (per-trade stake)
#   PAPER_MODEL       - default glm-5.2
#   PAPER_THINKING    - default auto (auto | max | none)

set -euo pipefail

# Required env
if [[ -z "${GLM_API_KEY:-}" ]]; then
  echo "$(date -Iseconds) GLM_API_KEY not set; aborting." >&2
  exit 2
fi
if [[ -z "${PAPER_REPO_DIR:-}" ]]; then
  echo "$(date -Iseconds) PAPER_REPO_DIR not set; aborting." >&2
  exit 2
fi

# Optional env with defaults
LOG_PATH="${PAPER_TRADES_LOG:-/var/lib/omni-analyst/paper_trades.jsonl}"
TARGET="${PAPER_TARGET_MARKETS:-50}"
THRESHOLD="${PAPER_THRESHOLD:-0.05}"
SIZE="${PAPER_SIZE_USD:-5.0}"
MODEL="${PAPER_MODEL:-glm-5.2}"
THINKING="${PAPER_THINKING:-auto}"

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_PATH")"

echo "$(date -Iseconds) === paper trader daily run starting ==="
echo "  log:       $LOG_PATH"
echo "  repo:      $PAPER_REPO_DIR"
echo "  target:    $TARGET markets"
echo "  threshold: $THRESHOLD"
echo "  size:      \$$SIZE per trade"
echo "  model:     $MODEL (thinking=$THINKING)"

cd "$PAPER_REPO_DIR"

# Scan: open new positions
echo "$(date -Iseconds) --- scan ---"
uv run python ops/paper_trade.py scan \
  --log "$LOG_PATH" \
  --target-markets "$TARGET" \
  --threshold "$THRESHOLD" \
  --size-usd "$SIZE" \
  --model "$MODEL" \
  --thinking "$THINKING" \
  --method "paper_daily_${MODEL}_${THINKING}"

# Resolve: close out positions whose markets resolved
echo "$(date -Iseconds) --- resolve ---"
uv run python ops/paper_trade.py resolve \
  --log "$LOG_PATH"

# Report: rolling summary
echo "$(date -Iseconds) --- report ---"
uv run python ops/paper_trade.py report \
  --log "$LOG_PATH"

echo "$(date -Iseconds) === paper trader daily run complete ==="
