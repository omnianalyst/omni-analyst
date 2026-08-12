# Polymarket Stage A + Paper Trader

This module tests whether an LLM-driven probability estimator can beat the
Polymarket crowd. **It does not place live orders.** It runs two distinct
kinds of test:

1. **Stage A backtest** (`ops/run_stage_a.py`) — historical calibration and
   P&L backtest against already-resolved markets. Confirms whether an edge
   existed in the recent past, with realistic per-category fees.

2. **Forward paper trader** (`ops/paper_trade.py` + `ops/paper_trade_cron.sh`)
   — scans live active markets daily, logs what the strategy WOULD have
   bought, resolves positions as markets close, and reports rolling P&L.
   Confirms whether the edge holds forward in time, on live data, with
   no capital at risk.

## Quick start (one-off testing)

```bash
uv pip install openai
export GLM_API_KEY=...

# one-shot Stage A backtest with P&L sweep
uv run python ops/run_stage_a.py --target-markets 100 --threshold-list "0.02,0.05"

# one-shot paper trader scan
uv run python ops/paper_trade.py scan --target-markets 30
uv run python ops/paper_trade.py report
```

## Daily paper trader deployment

The paper trader is **not** wired into the deployed app's scheduler. It runs
as a host-level cron job, decoupled from app redeploys. This is deliberate:

- A teploy redeploy can wipe the app's working directory; the JSONL log
  outside that directory survives.
- An app crash does not interrupt the daily collection.
- The trader has its own external API dependencies (Polymarket, GDELT,
  Jina, Zhipu) that should not be coupled to the app's lifecycle.

### One-time install on the host (e.g. Proxmox box)

```bash
# Persistent log directory (NOT inside the app's deploy path)
sudo mkdir -p /var/lib/omni-analyst
sudo chown $USER:$USER /var/lib/omni-analyst

# Env vars (in /etc/environment, or a systemd EnvironmentFile, or your shell rc)
# - GLM_API_KEY        - your Zhipu API key
# - PAPER_REPO_DIR     - absolute path to omni-analyst/app-v2

# Cron entry (crontab -e). Runs at 06:17 daily, off-peak for Polymarket.
17 6 * * * /path/to/omni-analyst/app-v2/ops/paper_trade_cron.sh >> /var/log/paper_trader.log 2>&1
```

### What the cron wrapper does

Each daily run executes three steps via `ops/paper_trade_cron.sh`:

1. `scan` — opens new paper positions on markets the LLM disagrees with
2. `resolve` — closes positions whose markets have resolved since the last run
3. `report` — prints rolling P&L summary to stdout (captured by cron's redirect)

### Tuning knobs (env vars on the host)

| Variable | Default | Meaning |
|---|---|---|
| `PAPER_TRADES_LOG` | `/var/lib/omni-analyst/paper_trades.jsonl` | JSONL log path — must be persistent storage |
| `PAPER_TARGET_MARKETS` | `50` | Active markets scanned per day |
| `PAPER_THRESHOLD` | `0.05` | Min `|llm_prob - market_price|` to open a position |
| `PAPER_SIZE_USD` | `5.0` | Per-trade stake |
| `PAPER_MODEL` | `glm-5.2` | Zhipu model name |
| `PAPER_THINKING` | `auto` | Thinking mode (`auto`, `max`, `none`) |

## What survives what

| Event | Code | Log | Schedule |
|---|---|---|---|
| App redeploy (teploy) | Yes (in repo) | Yes (outside deploy dir) | Yes (host cron, not app scheduler) |
| App crash | Yes | Yes | Yes |
| Host reboot | Yes | Yes | Yes (cron resumes) |
| Repo deleted | **Gone** | Yes | No (recreate cron entry) |
| `/var/lib` wiped | Yes | **Gone** | Yes (empty log restarts) |

The JSONL log is the only state that matters. Back it up weekly if you care
about the data: `cp /var/lib/omni-analyst/paper_trades.jsonl{,.$(date +%F).bak}`.

## Reading the output

After 30+ days of daily runs, the report block will produce numbers like:

```
Paper trader report
  open positions:   47
  closed positions: 132
  win rate:         71.2%
  gross P&L:        $+184.50
  fees:             $-12.30
  net P&L:          $+172.20
  avg ROI/trade:    +26.0%
  worst drawdown:   $18.40
```

That out-of-sample number is the real answer to "does the edge survive live."
Compare it to the in-sample Stage A backtest (`ops/run_stage_a.py --pnl-threshold`) —
if live P&L is materially lower than in-sample, the strategy is overfit and
should not be promoted to real capital.

## What this is NOT

- **No live order routing.** The paper trader logs `would-trade` entries to
  JSONL; placing actual Polymarket orders requires a Polymarket `Venue`
  implementation against the CLOB signing API, which is out of scope for
  Stage A and would be a separate work order.
- **No fill simulation.** The paper trader assumes the trade fills at the
  observed mid price. Real maker-only execution would have partial fills
  and missed entries that this log does not capture.
- **No investment advice.** Per Omni Analyst's terms: this is informational
  tooling, the maintainers are not registered advisers, past backtest
  performance does not predict future results.
