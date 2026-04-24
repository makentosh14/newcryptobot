# Bybit Crypto Trading Bot — Phase 1

A modular, async Python 3.11+ crypto trading bot for Bybit USDT perpetual futures.

**Current status: Phase 1 — paper-trading skeleton with live-mode scaffolding.**

This is NOT a guarantee-profit system. It is a disciplined framework with strict
risk management. Read `ARCHITECTURE.md` style notes in each file's docstring.

---

## Quick Start (Hetzner Cloud / Ubuntu)

```bash
# 1. System deps
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip

# 2. Clone / extract project
cd ~/crypto-bot

# 3. Virtualenv
python3.11 -m venv .venv
source .venv/bin/activate

# 4. Install deps
pip install -r requirements.txt

# 5. Configure
cp .env.example .env
nano .env   # fill in BYBIT_API_KEY, BYBIT_API_SECRET, etc.
#           # KEEP BYBIT_TESTNET=True for first runs.
#           # KEEP TRADE_MODE=paper.

# 6. First run (paper mode)
python main.py
```

Press `Ctrl+C` to shut down gracefully.

---

## Safety model

- **Default mode: paper.** No real orders sent.
- **Live trading is double-armed.** Requires:
  - `TRADE_MODE=live`
  - `ENABLE_LIVE_TRADING=True`
  - `I_ACCEPT_LIVE_RISK=True`

  All three must be true. Any missing → live orders are refused.
- **12 safety gates** must pass before any order (see `risk_manager.pre_trade_check`).
- **Exchange-side SL/TP** on live orders — bot crash won't leave naked positions.
- **Circuit breaker**: daily loss limit + loss-streak cooldown.

---

## Test Checklist (run in order)

Each test should be run manually. If a test fails, stop and fix before moving on.

### Test 1 — Startup test
```bash
python main.py
```
Expect:
- Logs "Bybit Crypto Bot starting"
- Logs config summary
- "Symbol registry refreshed: N symbols"
- "WS connected"
- Heartbeats every 60s
- `Ctrl+C` → "Shutting down…" → "Bye."

### Test 2 — Config validation
```bash
# With .env missing critical field:
echo "ACCOUNT_RISK_PER_TRADE_PCT=99" >> .env
python main.py
```
Expect: pydantic validation error, clean exit, no partial startup.

Undo: remove the bad line.

### Test 3 — Market data test
After `python main.py` runs for 30s, tail logs:
```bash
tail -f logs/bot.log | grep -E "(Symbol|HEARTBEAT|CANDIDATE)"
```
Expect: symbol count > 0, periodic scans, heartbeat confirms ws_connected=True.

### Test 4 — Scoring test
In a Python shell (with venv active):
```python
from market_data import Candle
from score import score_candles
# 200 dummy uptrend candles
candles = [Candle(i*60000, 100+i*0.1, 100+i*0.1+0.2, 100+i*0.1-0.1, 100+i*0.1+0.05, 1000) for i in range(200)]
s = score_candles(candles)
print(s.direction, s.total, s.reasons[:3])
# Expect: LONG with high score
```

### Test 5 — Paper trade test
Run `python main.py` in paper mode. Wait for a CANDIDATE log line.
If the score passes threshold, you'll see:
- `Executing PAPER: ...`
- `Paper OPEN ...`
- Later: `Paper CLOSE ... pnl=...`

Check `logs/bot.log` for the full lifecycle.

### Test 6 — Live safety test (CRITICAL)
1. Set `TRADE_MODE=live` but keep `ENABLE_LIVE_TRADING=False`.
2. Run `python main.py`.
3. Expect warning: "TRADE_MODE=live but safety flags not both True — orders will be BLOCKED."
4. Even if a signal fires, no real order should be placed. Look for rejection log: `live mode not fully armed`.

### Test 7 — WS resilience
1. While bot runs, drop network for 30s (e.g., `sudo iptables -A OUTPUT -d stream.bybit.com -j DROP`).
2. Observe: logs show "WS connection closed" then "WS reconnecting in Ns" with exponential backoff.
3. Restore network: `sudo iptables -D OUTPUT -d stream.bybit.com -j DROP`.
4. Expect: WS reconnects, subscriptions restored.

### Test 8 — Telegram alert
1. Set `TELEGRAM_ENABLED=True`, `TELEGRAM_BOT_TOKEN=...`, `TELEGRAM_CHAT_ID=...` in `.env`.
2. Start bot; expect a "🤖 Bot started" message.
3. Disable internet; bot should keep running (alerts queued/dropped, no crash).

### Test 9 — Graceful shutdown
1. Start bot.
2. `Ctrl+C` once.
3. Expect within 5s:
   - "Shutdown signal received"
   - "PositionMonitor stopped"
   - "WebSocketManager stopped"
   - "Bye."

---

## What's in Phase 1

- ✅ Centralized config + .env
- ✅ Structured logging (console + rotating files)
- ✅ Bybit REST client (pybit wrapper, async)
- ✅ Symbol registry with precision filters
- ✅ WebSocket kline manager with reconnect
- ✅ In-memory candle cache + REST fallback
- ✅ Pure numpy indicators (EMA, RSI, ATR, MACD, BB)
- ✅ Transparent scoring engine (trend/momentum/volume/volatility/structure)
- ✅ Risk manager with 12-rule safety gate
- ✅ Circuit breaker (daily loss + loss-streak cooldown)
- ✅ Paper broker with slippage + fee model
- ✅ Live broker (blocked unless double-armed)
- ✅ Position monitor (SL/TP detection, break-even after TP1)
- ✅ Non-blocking Telegram alerts
- ✅ Graceful shutdown

## What's deferred to Phase 2

- ⏳ SQLite trade journal + equity curve
- ⏳ Reconciler on startup (re-adopt open exchange positions)
- ⏳ Full scanner across all linear USDT perpetuals
- ⏳ Setup builder with structure-based SL (not just ATR multiple)
- ⏳ Multi-timeframe confluence scoring
- ⏳ Backtest engine
- ⏳ Learning module (post-hoc analysis)
- ⏳ Telegram commands (/pause, /status, /close)

---

## Hetzner deployment notes

- Enable NTP so system clock doesn't drift (Bybit signature requires it):
  ```bash
  sudo timedatectl set-ntp true
  ```
- Run under `systemd` so it restarts on crash. Example unit file:
  ```ini
  [Unit]
  Description=Bybit Trading Bot
  After=network-online.target

  [Service]
  Type=simple
  User=bot
  WorkingDirectory=/home/bot/crypto-bot
  ExecStart=/home/bot/crypto-bot/.venv/bin/python main.py
  Restart=on-failure
  RestartSec=10
  StandardOutput=append:/home/bot/crypto-bot/logs/systemd.log
  StandardError=append:/home/bot/crypto-bot/logs/systemd-err.log

  [Install]
  WantedBy=multi-user.target
  ```

---

## Honest warnings

- Past performance ≠ future results.
- Paper results will be better than live (slippage, funding, queue position).
- Do not run two instances on one Bybit account.
- Test on testnet before risking real capital.
- Even correct code can lose money in bad market conditions.
