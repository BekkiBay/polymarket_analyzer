# 🦢 Black Swan Hunter | Polymarket

> Automated trading bot for [Polymarket](https://polymarket.com) — hunts for undervalued outcomes using a multi-stage AI pipeline.

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white)
![Polymarket](https://img.shields.io/badge/Polymarket-Gamma%20API-6c47ff)
![Gemini](https://img.shields.io/badge/Google-Gemini%20Flash-4285F4?logo=google&logoColor=white)
![Claude](https://img.shields.io/badge/Anthropic-Claude%20Sonnet-D4A017)
![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?logo=telegram&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

---

## What is this?

Black Swan Hunter scans Polymarket every hour, filters thousands of prediction markets through a 4-level pipeline, and sends Telegram alerts when it finds statistically undervalued outcomes — markets where the crowd is likely wrong.

The bot operates in **two distinct modes**:

| Mode | Strategy | Markets | Bet size |
|------|----------|---------|----------|
| 🦢 **Sniper** | Rare, asymmetric black swans | Geopolitics, macro, crypto risk | $0.10–$1.00 |
| ⚡ **Conveyor** | High-volume underdog plays | Sports, esports | $0.10–$0.50 |

---

## Core Concept

The strategy exploits two well-documented market inefficiencies:

1. **Favourite-Longshot Bias** — prediction markets systematically underprice tail risks. A 3% outcome may have a true probability of 6–8%.
2. **Attention asymmetry** — geopolitical events outside the Polymarket audience's focus are mispriced. Nobody is watching, so nobody is correcting.

**Expected value formula:**
```
EV = P_real × (1/P_market - 1) - (1 - P_real)
```
When `P_real > P_market × 1.5` and `EV > 0` — it's a candidate.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    main.py                          │
│         Scan loop (every 60 min)                    │
└──────┬──────────────────────────────────────────────┘
       │
       ▼
┌─────────────┐    ┌──────────────┐    ┌─────────────┐
│  scanner.py │    │  filters.py  │    │ ai_analyst  │
│             │───▶│              │───▶│    .py      │
│ Gamma API   │    │  4-level     │    │             │
│ tag-based   │    │  pipeline    │    │ Gemini Flash│
│ scan        │    │              │    │ + Claude    │
└─────────────┘    └──────────────┘    └─────────────┘
       │                  │                   │
       ▼                  ▼                   ▼
┌─────────────┐    ┌──────────────┐    ┌─────────────┐
│    db.py    │    │  alerter.py  │    │  journal.py │
│             │    │              │    │             │
│  SQLite     │    │  Telegram    │    │  Telegram   │
│  storage    │    │  alerts      │    │  bot cmds   │
└─────────────┘    └──────────────┘    └─────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────┐
│               panic_monitor.py                      │
│  🚨 Panic detector  │  🦅 Vulture mode (price spikes)│
└─────────────────────────────────────────────────────┘
```

---

## Filter Pipeline

Markets go through 4 progressive levels before an alert is sent:

```
~20,000 raw markets
        │
        ▼ Level 1 — Price filter
        │   🦢 Sniper:   $0.01–$0.10 per share
        │   ⚡ Conveyor: $0.01–$0.15 per share
        │
        ▼ Level 2 — Liquidity & time
        │   🦢 Sniper:   volume ≥ $500, 1–90 days to expiry
        │   ⚡ Conveyor: volume ≥ $100, event within 48h
        │
        ▼ Level 3 — Keyword filter (Sniper only)
        │   Blacklist: sports/entertainment keywords
        │   Whitelist: geopolitics/macro/crypto keywords
        │
        ▼ Level 4 — AI classification (Gemini Flash)
        │   Stage 1 — classify(): no internet, text only
        │     🦢 3 criteria: asymmetry + mechanism + crowd undervaluation
        │     ⚡ 3 criteria: non-tier1 match + real underdog chances + mispricing reason
        │   Stage 2 — score(): DuckDuckGo search + Gemini
        │     Returns: score/10, estimated_prob%, reasoning, key_catalyst
        │
        ▼ ~5–15 candidates per cycle
        │
     🟢 GREEN → instant Telegram alert
     ⚪ GRAY  → daily digest at 21:00 UTC
```

---

## Special Modes

### 🦅 Vulture Mode
Detects markets where the price has **jumped 50%+ in 24h** on cheap markets:
- `$0.02 → $0.03+` from price history  
- OR `oneDayPriceChange ≥ 2pp` from API  
- Triggers immediate AI scoring + dedicated alert

### 🚨 Panic Monitor
Watches large markets (volume > $10k) for:
- **Single panic**: price change ≥ 15pp in 1 hour
- **Cluster panic**: 3+ markets in same category moving 10pp+ in a day

---

## AI Pipeline

```
classify(market)          ← no internet, cached 24h
    │
    ├── is_candidate = false → rejected (logged, not alerted)
    │
    └── is_candidate = true
            │
            ▼
        score(market)     ← DuckDuckGo search, cached 12h
            │
            ├── Gemini Flash (primary)
            └── Claude Sonnet (fallback via proxy/OpenRouter)
                    │
                    ▼
              score/10 + estimated_prob% + reasoning + key_catalyst
```

**Deep Analysis** (`/analyze` command): Claude Sonnet full analysis with:
- Current situation assessment
- Factors for/against
- Probability estimate vs market price
- Spread analysis
- Actionable recommendation

---

## Installation

### Requirements
- Python 3.11+
- Telegram bot token ([BotFather](https://t.me/botfather))
- Google Gemini API key ([ai.google.dev](https://ai.google.dev))
- Anthropic API key ([anthropic.com](https://anthropic.com)) — optional, Gemini covers all functions

### Setup

```bash
git clone https://github.com/yourusername/polymarket_analyzer
cd polymarket_analyzer

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration

Edit `config.py` — all settings in one place:

```python
# Telegram
TELEGRAM_BOT_TOKEN = "your_bot_token"
TELEGRAM_CHAT_ID   = "your_chat_id"

# AI
GEMINI_API_KEY    = "your_gemini_key"       # Required
ANTHROPIC_API_KEY = "your_anthropic_key"    # Optional

# If running on a VPS/Russian server — Anthropic blocks these IPs
# Option A: HTTP proxy
CLAUDE_PROXY = "http://user:pass@host:port"
# Option B: OpenRouter (https://openrouter.ai)
OPENROUTER_API_KEY = "sk-or-v1-..."

# Budget split between modes
SNIPER_BUDGET_PCT   = 50   # 50% → geopolitics/macro
CONVEYOR_BUDGET_PCT = 50   # 50% → sports/esports
```

### Run

```bash
# Foreground (development)
python3 main.py

# Background (production)
nohup python3 main.py >> black_swan.log 2>&1 &
echo "PID: $!"

# Watch logs
tail -f black_swan.log
```

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/bet {id} {$} {reason}` | Record a bet |
| `/skip {id}` | Skip a market (logged for calibration) |
| `/analyze {id}` | Deep AI analysis (Claude/Gemini) |
| `/portfolio` | Active bets with 🦢/⚡ breakdown |
| `/budget` | Budget by mode — sniper vs conveyor |
| `/stats` | P&L, win rate, AI accuracy — split by mode |
| `/review` | Closed bets review |
| `/ping_ai` | Diagnose Gemini + Claude API connectivity |
| `/help` | Command list |

---

## Alert Formats

### 🦢 Sniper — Black Swan Alert
```
🦢 BLACK SWAN ALERT

📌 Will Iran strike Israel before April?
💰 Price: $0.032 (3.2%)
🎯 Outcome: Yes
📊 Volume: $12,450 | Spread: 15%
🏷️ Topic: 🌍 geopolitics
⏰ Expires: 2026-03-31 (20 days)

🤖 AI SCORE: 🔥 8/10
📈 AI estimate: 7.5% (market: 3.2%)
💬 Recent escalation near Syria border...
🎯 Catalyst: US withdrawal announcement

🔗 Open on Polymarket

/bet 0xa71e... 0.20 escalation risk
/analyze 0xa71e...
```

### ⚡ Conveyor — Sports Underdog
```
⚡ CONVEYOR BET

🎮 Team Liquid vs NaVi — Map 1 winner?
💰 Underdog: $0.08 (8%)
📊 Volume: $3,200 🎮 esports
⏰ Match in: 6h

🤖 AI: ⚡ 7/10
📈 Estimate: 18% (market: 8%)
💬 NaVi missing star player, Liquid on 5-win streak
🎯 Key factor: roster change + map pool advantage

/bet 0xb33f... 0.30 roster_change
/skip 0xb33f...
```

### 🦅 Vulture — Price Spike
```
🦅 СТЕРВЯТНИК — ЦЕНА РАСТЁТ

📌 Will Hamas release hostages by March 20?
💰 Было: $0.020 → Стало: $0.045 (+125%)
📊 Volume 24h: $8,900
```

---

## Project Structure

```
black_swan_hunter/
├── main.py           # Entry point, scan loop orchestration
├── config.py         # All settings — edit this file only
├── scanner.py        # Polymarket Gamma API, tag-based scanning
├── filters.py        # 4-level filter pipeline (sniper + conveyor)
├── ai_analyst.py     # Gemini/Claude classification & scoring
├── alerter.py        # Telegram message formatting & sending
├── journal.py        # Telegram bot commands, bet journal
├── db.py             # SQLite operations (markets, bets, AI cache)
├── panic_monitor.py  # Panic detection + Vulture mode
└── requirements.txt
```

---

## Database Schema

```sql
markets          -- all scanned markets with prices, volumes, mode
ai_classifications -- AI results: stage=classify|score, cached 24h/12h
bets             -- bet journal: entry_price, ai_score, mode, P&L
skips            -- skipped markets (calibration data)
alerted_markets  -- deduplication: prevents duplicate alerts
price_history    -- price snapshots for Vulture mode detection
deep_analyses    -- /analyze results history
```

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11+ |
| Database | SQLite (built-in, zero deps) |
| HTTP | `requests` with retry logic |
| AI — Classification | Google Gemini Flash |
| AI — Deep Analysis | Anthropic Claude Sonnet (via proxy/OpenRouter on VPS) |
| News Search | DuckDuckGo (`ddgs`) |
| Telegram | Bot API (long polling, no frameworks) |
| Scheduling | Custom loop with `time.sleep` |
| Concurrency | `threading` (bot polling in daemon thread) |

---

## Risk Management

- **Budget split**: 50% Sniper / 50% Conveyor, configurable
- **Per-bet limits**: Sniper max $1.00 / Conveyor max $0.50
- **Weekly limit**: configurable `WEEKLY_BUDGET_LIMIT`
- **AI gate**: only markets scoring ≥ `SNIPER_MIN_AI_SCORE` / `CONVEYOR_MIN_AI_SCORE` get GREEN alerts
- **AI cost cap**: `AI_MAX_PER_CYCLE = 200` — max new AI calls per scan cycle
- **Human override**: every alert requires manual `/bet` command — bot never bets automatically

> ⚠️ **This bot does NOT place bets automatically.** It only sends alerts. All bets are recorded manually via `/bet` command for full human control.

---

## Performance Expectations

Based on the Black Swan strategy theory:

| Metric | Expectation |
|--------|------------|
| Win rate | ~15–25% (most black swans don't happen) |
| Average payout when win | 15–30x (buying at 3–7¢) |
| Breakeven win rate | ~1/payout ratio (~5–7%) |
| Expected edge | Positive EV if AI score ≥ 7 |

One correct black swan call (e.g., $0.20 bet at 3¢ that resolves YES at $1) returns **$6.60 profit** — covering ~33 losing bets at $0.20 each.

---

## ⚠️ Disclaimer

This project is for **educational and research purposes**. Prediction market trading involves financial risk. Past performance does not guarantee future results. Always do your own research before placing any bets.

---

## License

MIT — use freely, attribution appreciated.

---

*Built with Python, powered by Gemini + Claude, hunting for what the crowd misses.*
