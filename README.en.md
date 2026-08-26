# MyStock HTS (Home Trading System)

[Korean](README.md) | [English](README.en.md)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

A **terminal-based stock trading and analysis system** built on the Korea Investment & Securities (KIS) and Toss Securities Open APIs.
Real-time quotes, technical analysis, strategy-driven automated trading, backtesting, and AI reports — all in one CLI.

> **The operating principle is trend following.** Cut losses fast and let winners run until the trend breaks (no fixed take-profit; a Chandelier trailing stop is the primary exit). Settings that contradict this principle (fixed take-profit, half take-profit, RSI-overheat selling, mean-reversion buying) remain in the code but are **disabled by default and hidden from the settings menu.**

### Disclaimer

> This software is for learning and research purposes and is only an aid to investing. **You alone are responsible for any investment losses.**
> API outages, network instability, or bugs may cause missed, duplicated, or mispriced orders. In an emergency, stop the program immediately and act through your broker's own HTS/MTS.
> Always validate thoroughly in **paper trading (mode 1)** before going live.

---

## Table of Contents

| # | Section | # | Section |
|---|---|---|---|
| 1 | [Feature Overview](#1-feature-overview) | 6 | [Environment Variables & Credentials](#6-environment-variables--credentials) |
| 2 | [Trading Strategy](#2-trading-strategy) | 7 | [Project Structure](#7-project-structure) |
| 3 | [Risk Management](#3-risk-management) | 8 | [Architecture & Stability](#8-architecture--stability) |
| 4 | [Key Settings](#4-key-settings) | 9 | [Telegram Bot](#9-telegram-bot) |
| 5 | [Installation & Execution](#5-installation--execution) | 10 | [Reserved Orders · Journal · Known Issues](#10-reserved-orders) |

---

## 1. Feature Overview

Quotes, orders, analysis, and automated trading all run in the terminal. Each menu number is a functional unit.

| Menu | Feature | Details |
|---|---|---|
| `[0]` | Settings | Buy/sell strategy, scoring & market regime, risk & allocation, indicator parameters, environment/system. **Changes apply immediately while running and persist across restarts.** |
| `[1]` | Market Indices | Domestic/US/Europe/Asia indices, sector indices, commodities (gold, silver, copper, oil), FX, US Treasury yields, crypto, and **KRX spot gold**. Index name color encodes the regime (Bull / PendUp / PendDown / Bear). |
| `[2]` | Stock Analysis | Combines real-time quotes and technical indicators for domestic/US stocks and ETFs into a state: **Buy / Wait / Rise / Interest / Neutral / Caution / Sell**. Per-index deep analysis included. |
| `[3]` | Chart Analysis | Weekly/daily/hourly/intraday chart images, plus **AI chart image reading** (Gemini Vision). |
| `[4]` | Backtesting | Single stock / **N-slot watchlist portfolio** / **Monte Carlo** (±1% price noise, slippage variance, fill misses, 1,000 runs → mean, VaR 95%, stdev) / **Walk-Forward** validation. Optimization over buy score, RSI cap, stop width, pyramiding depth, and scoring weights runs alongside. |
| `[5]` | System Trading | Start/stop/status/report/log for the auto-trader, **per-symbol trading rules**, and **restricted symbols**. |
| `[6]` | Discovery & Financials | Naver theme ranking, **TradingView screener** (9 presets: top movers, gap-up, breakout, pullback, volume momentum, oversold rebound, value turnaround, high dividend, trend reversal — KR/US), AI theme analysis, AI stock diagnosis, investment calendar, disclosure monitoring, supply/overhang signals, financial snapshot. |
| `[7]` | Watchlist | Add/remove/view/edit/memo for domestic & US stocks and ETFs, plus **candidate discovery** (rule-based screening with multi-select add). |
| `[8]` | Order Management | Buy, sell, modify, cancel, and **9 types of reserved orders**. |
| `[9]` | Assets | Asset summary, holdings, trade history (Excel export), trade evaluation, **position analysis** (direct entry handles unlisted assets — enter ticker `KRXGOLD` to register spot-gold holdings in grams and see ATR stop / trailing levels), **paper account management**. |

### Three Operating Modes

Chosen at startup or via `--mode`. Each mode keeps a separate config profile, so **settings used for validation never leak into live trading.**

| Mode | Name | Quotes | Orders | Purpose |
|---|---|---|---|---|
| `1` | Paper (Observation) | KIS live | Simulated fills | Long-run observation on live quotes — real orders blocked at the source, separate DB file |
| `2` | KIS Live | KIS live | **Real orders** | Live account |
| `3` | Toss Live | Toss | **Real orders** | Live account |

> The KIS simulation mode was retired on 2026-08-26. Its server is capped at 2 TPS and the
> account must be reissued every three months, and with no NXT/SOR support plus an open-order
> query bug its conditions diverged from live trading. **Mode 1 (paper) replaces it** — quotes,
> indicators, and decisions take the same path as live; only fills are simulated. The old
> `--mode 4` still works but warns and runs as mode 1.

### Highlights

**Trading & Quotes**
- Unified management of domestic/US stocks and ETFs; real-time quotes with buy/sell/modify/cancel orders
- **NXT (alternative exchange) & SOR support** — quotes and orders during pre-market (08:00–08:50) and after-hours (15:30–20:00). Trading pauses during the KRX single-price auctions (08:50–09:00 and 15:20–15:30). Note that **the auto-trader's default window is the KRX regular session, 09:00–15:30** (all indicators and daily bars are on a KRX basis). To extend, widen the trading hours to `0800`/`2000` under `[0] → 5-1`.
- **US day market** — uses KIS's dedicated exchange codes (`BAQ`/`BAY`/`BAA`) for the overnight ATS session, falling back to regular-session codes when there are no fills.
- **KRX close after hours** (`USE_KRX_CLOSE_AFTER_HOURS`, ON by default) — once every session has closed, the current price and indicators are pinned to the KRX regular-session close, so a stale last NXT print cannot move indicators built on KRX daily bars. **Order prices always use the live price regardless of this setting.**
- **9 reserved-order types** — stop, breakout, limit, time, quant score, RSI, trailing buy/sell, EMA cross. Watched every 3 seconds in the background and persisted to the DB.

**Analysis & Discovery**
- Quant multi-factor scoring (10 points) with 7-state classification
- Per-symbol trading rules (score, RSI, stop-loss, trailing ratios)
- Candidate discovery — instead of letting a human pick "promising" names, it **filters by rule and fills by breadth** (excluding empirically weaker groups such as defensive sectors and holding companies), based on audit findings that a wider candidate pool keeps slots from idling
- Naver theme ranking crawler, TradingView screener integration

**AI Assistant (Gemini)**
- Stock deep-dive / index deep-dive / chart image reading (Vision) / market theme analysis / pre-market briefing / closing briefing / watchlist curation / conversational Q&A / backtest evaluation / trade autopsy
- **Safeguards**: ① every prompt carries the trend-following stance so counter-trend advice (fixed take-profit, averaging down) is not generated ② proposals already rejected by backtests are blocked from being re-suggested ③ reports state a confidence level and the **falsification condition** ④ ticker codes written by the AI are cross-checked against the KRX listing to flag non-existent codes or sub-threshold market caps
- Note: **Search Grounding (real-time web search) is not used.** Up-to-date inputs (macro indicators, investor flows, disclosures) are collected by the system and placed in the prompt.

**Disclosures (DART)**
- Disclosure monitoring (severity classification, AI positive/negative reading, automatic extraction of supply contracts, treasury stock, bonus issues, capital reduction, CB/BW)
- Investment calendar (key economic events plus expected ex-dividend dates, confirmed record dates, expected earnings dates)
- Supply/overhang signals (treasury-stock decisions, mezzanine overhang, insider and 5%-holder net changes)
- Financial snapshot (revenue/operating/net income changes, standalone quarterly results, ROE, debt ratio)
- **Automatic Telegram alerts** for material disclosures and same-day calendar items

**Operations**
- Telegram notifications and remote control (30+ commands)
- **Circuit breaker (CB) and VI alerts** based on exchange status flags
- Live settings changes applied instantly; restricted-symbol management
- **Trading journal web sync** (outbox pattern plus a two-stage backfill)
- **Process death detection** by an external cron watcher — it only notifies and never auto-restarts
- Memory caps and cache eviction for long unattended runs on a Raspberry Pi (1 GB)

---

## 2. Trading Strategy

> The auto-trader operates only on the **domestic stocks** in your watchlist (domestic ETFs are opt-in via `SYSTEM_INCLUDE_ETF`).

### Entry — every gate must pass

| # | Condition | Default |
|---|---|---|
| 1 | Total score ≥ `BUY_SCORE` | 7.0 |
| 2 | RSI < `BUY_RSI_MAX` (relaxed under super momentum) | 70 |
| 3 | Volume strength ≥ `BUY_VOL_STRENGTH` | 100% |
| 4 | Index above its moving average or within the deviation band (`MARKET_FILTER_MA`/`BAND`) | 80d / 1% |
| 5 | Trend quality < `TREND_QUALITY_MAX` (blocks momentum crashes) | 300 |

- **Entry ranking**: ① total score → ② trend quality → ③ proximity to the 52-week high → ④ volume strength.
  Trend quality is the annualized slope × R² of a log-close linear regression over 90 days (Clenow momentum); it breaks score ties, which occur on 25–32% of contested slot days.
- **Why trend quality has a ceiling**: higher is not better. Above 300 lies the **momentum-crash** zone right after a vertical run, where 20-day forward returns turn negative and the right tail is cut off.
- **Super momentum**: leaders near their 52-week high get a relaxed RSI cap so the trend can be ridden further.
- **Pyramiding**: only positions already up `PYRAMIDING_PROFIT_TRIGGER` (+10%) with an intact buy signal are increased (50% of held quantity, up to 3 times). **Averaging down is structurally impossible**, and the added tranche's stop is recomputed from the ATR at that moment and folded into the weighted-average stop.

### Exit — evaluated in priority order

1. **Stop loss** — ATR-based dynamic stop (`USE_ATR_STOP`, ON). **Break-even stop**: once peak profit clears the threshold, the stop is raised to +0.5%.
2. **Time stop** — after `TIME_STOP_DAYS` (15), only names still **at a loss** with no upside momentum are cleared (winners are never sold on time alone).
3. **Trailing stop (primary exit · Chandelier)** — arming is set by the symbol's own volatility (break-even linked), then it sells on a dynamic callback from the high. **The arming multiplier and the callback multiplier are separate**, so arming earlier never narrows the exit line.
4. **Trend break** — full exit when the score falls below `SELL_SCORE` (4.0) **and** price loses the 60-day line (structural damage). A score drop alone does not sell, so ordinary pullbacks inside an aligned trend do not trigger early exits.

> **Disabled by default** (upside-capping options — hidden from the menu, editable only in `json/dynamic_config.json`): fixed take-profit, half take-profit, RSI-overheat selling, defensive half-sell, mean-reversion entries.

### Scoring (10 points, 0.5 increments)

| Factor | Weight | Components |
|---|---|---|
| **Trend** | 4.0 | MA alignment (EMA cluster capped at 2.0), early trend reversal, trend persistence (≥70% of the last 120 days above the 60-day line), MACD, SAR |
| **Momentum** | 2.5 | RSI bands, CCI, DMI cross, **price momentum** (6-month return + 52-week position ≥ 80%, withheld if 1M/3M are negative) |
| **Strength & Volume** | 1.5 | ADX ≥ 20, volume surge with an up candle, OBV / smart-money turnaround |
| **Synergy** | 2.0 | Trend start (above 60-day line + MACD positive + ADX ≥ 20), momentum burst (MACD positive + RSI ≥ 60 + OBV rising) |

| State | Condition | Meaning |
|---|---|---|
| **Buy** | score ≥ 7.0 and all gates pass | Enter |
| **Wait** | score ≥ 7.0 but RSI overheated | Waiting for a pullback (auto-promotes to Buy as RSI cools) |
| **Rise** | 6.0 ≤ score < 7.0 | Trend alive but below threshold |
| **Interest** | early reversal signals ≥ `INTEREST_SIGNAL_MIN` (3) | Manual swing monitoring; not an auto-buy target |
| **Caution** | past the overheat line | Risky |
| **Sell** | score < 4.0 and 60-day line lost | Exit |

---

## 3. Risk Management

**Position sizing — the min of three caps**
The order amount is the **smallest** of ① the base weight (concentration control) ② the risk-based cap (loss control) ③ the volatility cap (volatility normalization). ② and ③ are not multiplied because both are inversely proportional to ATR when ATR stops are used, so multiplying would shrink size as `1/ATR²`.

- **Max holdings with auto-linked weight** — with per-symbol weight at its default `0` (auto), the weight is `1 ÷ slots`, so changing only the slot count keeps the notional sum at 100%. A positive value allows a cash buffer (4 × 0.2 = 80%) or deliberate overcommitment.
- **Risk-based sizing** — caps single-trade loss against account equity (4% default); wider stops automatically get smaller positions.
- **Portfolio heat cap** — limits the summed loss if every holding fell to its effective stop simultaneously, to 10% of equity (`SYSTEM_MAX_PORTFOLIO_RISK`).
- **Volatility targeting** — normalizes annualized volatility to a 25% target via ATR, clamped so the scale-up never exceeds the base weight.
- **Dynamic risk scaling** — ① regime-linked (×0.6 in PendDown) ② whipsaw-rate linked (smaller in choppy markets) ③ stepped drawdown deceleration (−5% ×0.75, −10% ×0.5) ④ gap-risk buffer (stop width ×1.2). It only shrinks entry size and never touches exit logic.

**Entry blocks & defenses**

| Guard | Behavior |
|---|---|
| Market index filter | Blocks new buys when the index falls more than 1% below its SMA80; resumes after recovering the same band. The backtest applies the identical rule |
| Direction unknown | If index data cannot be fetched, new buys are **held, not allowed** (fail-closed). Sell/stop paths never consult index state, so they keep working during outages |
| Correlation filter | Blocks a new buy correlated ≥ 0.7 with existing holdings (prevents portfolio concentration) |
| Two-stage technical filter | Holds buys on trend damage; **panic-selling zones (ADX ≥ 45 with -DI dominant) are blocked outright regardless of score** |
| Daily loss limit | If equity falls past the limit (−10%), **only new buys and pyramiding stop**. Exit monitoring keeps running and the block clears when the day rolls over |
| No stop, no entry | If neither ATR nor fixed stop is available, entry is skipped (never enter without an exit plan) |
| Pyramiding market gate | In a weak market, even a proven winner is not increased |
| Consecutive-error kill switch | Five consecutive API/system errors put the system into standby |
| Alert for exempt positions | Restricted symbols and ETFs outside auto-trading are not auto-sold — breaching the stop raises a **Telegram alert** instead (24h throttle) |

**Adaptive thresholds (market regime)**
A dual-EMA rule: the fast EMA (9) crossing the slow EMA (41) counts as a confirmed trend only after the index advances **5% past the cross**. Reversals before confirmation are counted as whipsaws.

| Regime | Definition | Buy threshold |
|---|---|---|
| Bull | Up-cross confirmed by +5% | Relaxed |
| PendUp | Up-cross, below 5% | Unchanged |
| PendDown | Down-cross, above −5% = **early trend breakdown** | Tightened |
| Bear | Down-cross confirmed by −5% | Tightened |

**Slippage** — 0.2% by default. Backtests apply it adversely; live orders shift the price toward a higher fill probability. (Large caps/ETFs 0.1–0.2% · KOSDAQ 0.3–0.5% · fast movers 0.5–1.0%)

---

## 4. Key Settings

`config.py` holds the defaults. Values changed under `[0] Settings` are saved to `json/dynamic_config.json` and survive restarts. Per-symbol overrides live under `[5] → 6. Per-symbol trading rules`.

| Group | Key | Default | Description |
|---|---|---|---|
| **Indicators** | `CHART_LOOKBACK_DAYS` | 730 | Lookback (headroom for the 120-day MA) |
| | `RSI_PERIOD` / `RSI_UPPER` / `RSI_LOWER` | 14 / 70 / 30 | RSI |
| | `SAR_AF_START` / `STEP` / `MAX` | 0.02 / 0.02 / 0.2 | Parabolic SAR acceleration |
| | `CCI_WINDOW` / `CCI_UPPER` / `CCI_LOWER` | 20 / +100 / −100 | CCI |
| | `ADX_PERIOD` | 14 | ADX (≥20 means a trend is present) |
| | `OBV_MA_PERIOD` | 10 | OBV moving average |
| **Entry** | `BUY_SCORE` / `RISE_SCORE` | 7.0 / 6.0 | Buy and rise thresholds |
| | `BUY_RSI_MAX` | 70 | RSI ceiling for entries |
| | `BUY_VOL_STRENGTH` | 100.0 | Minimum volume strength (%) |
| | `INTEREST_SIGNAL_MIN` / `INTEREST_MA60_NEAR` | 3 / 0.97 | "Interest" classification |
| | `TREND_QUALITY_MAX` / `LOOKBACK` | 300 / 90 | Trend-quality cap and window |
| | `SUPER_MOMENTUM_SCORE` / `W52_POS` / `SUPER_BUY_RSI_MAX` | 8.0 / 90% / 80 | Super momentum trigger and relaxation |
| | `PYRAMIDING_PROFIT_TRIGGER` / `RATIO` / `MAX_COUNT` | +10% / 0.5 / 3 | Pyramiding |
| **Exit** | `STOP_LOSS_RATE` | −7.0% | Fixed stop |
| | `USE_ATR_STOP` / `ATR_STOP_MULTIPLIER` | True / 2.0 | ATR stop |
| | `MAX_ATR_STOP_LOSS_RATE` | −15.0% | Safety cap on stop width |
| | `BREAK_EVEN_PROFIT_RATE` / `STOP_RATE` | 5.0% / +0.5% | Break-even stop |
| | `TIME_STOP_DAYS` / `MIN_PROFIT_RATE` | 15d / 0.0% | Time stop |
| | `TS_ACTIVATION_MODE` / `TS_ACTIVATION_ATR_MULTIPLIER` | breakeven / 3.0 | Trailing arming |
| | `TRAILING_ATR_MULTIPLIER` / `CALLBACK_RATE` | 3.5 / 5.0% | Trailing callback (whichever is larger) |
| | `SELL_SCORE` | 4.0 | Trend-break exit (with the 60-day line lost) |
| **Risk** | `SYSTEM_MAX_HOLDINGS` | 4 | Max holdings (slots) |
| | `SYSTEM_INVEST_PER_STOCK` | 0 (auto) | Per-symbol weight — `1÷slots` when 0 |
| | `SYSTEM_RISK_PER_TRADE` | 4% | Max loss per trade |
| | `SYSTEM_MAX_PORTFOLIO_RISK` | 10% | Portfolio heat cap |
| | `TARGET_VOLATILITY` | 25% | Volatility target |
| | `GAP_RISK_BUFFER` | 1.2 | Gap-risk buffer |
| | `MARKET_FILTER_MA` / `BAND` | 80d / 1% | Market filter |
| | `USE_CORRELATION_FILTER` / `CORRELATION_THRESHOLD` | True / 0.7 | Correlation filter |
| | `SLIPPAGE_RATE` | 0.002 | Slippage |
| **Scoring** | `TREND`/`MOMENTUM`/`STRENGTH`/`SYNERGY` | 4.0 / 2.5 / 1.5 / 2.0 | Factor weights |
| **Regime** | `REGIME_EMA_FAST`/`SLOW`/`CONFIRM_PCT` | 9 / 41 / 5% | Regime detection |
| | `BULL_SCORE_ADJ` / `PENDING_DOWN_SCORE_ADJ` | −0.5 / +0.5 | Per-regime threshold adjustment |

> Slot count follows the seed: ~2M KRW → 3 · 3M–10M → 4 · 15M–50M → 5 · 50M+ → 6. Sizing scales with equity, so a larger seed keeps the same exposure ratio and only raises the amounts.

---

## 5. Installation & Execution

**Requirements**: Python 3.10+, a brokerage account and API keys.

```bash
# 1) Get the source
git clone https://github.com/your-username/my-stock-hts.git
cd my-stock-hts

# 2) Virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3) Dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt   # for development and tests

# 4) Run
chmod +x run.sh                    # first time only
./run.sh                           # interactive menu
./run.sh --help                    # options
./run.sh --mode 1 --auto           # start auto-trading immediately in paper mode
```

- `run.sh` activates the virtual environment and installs dependencies for you. **`requirements.txt` is the single source of truth** for dependencies, and `run.sh` reads that file.
- The `holidays` package is **not** auto-upgraded at startup — if the holiday calendar changed silently on every launch, trading-hour decisions would change without anyone noticing. Run `tools/update_holidays.sh` from cron weekly instead.
- **One instance per mode, per host.** A second launch names the process already holding the mode and exits — two instances fight over Telegram polling (409), the KIS rate/websocket/token budget, and the same DB file. Add `--allow-duplicate` for a read-only second instance (the account lock still blocks live orders).
- Use `run.bat` on Windows; for always-on Linux hosts see `tools/stock-hts` (tmux session setup).

---

## 6. Environment Variables & Credentials

Secrets live in **environment variables**. Put them in a shell profile such as `~/.htsrc` with `export`; `run.sh` sources that file (a `.env` file also works — see `.env.example`).

> **After adding or changing a variable, re-source the shell (`source ~/.htsrc`) and restart the program.** A process that is already running will not pick up new variables. The startup pre-flight prints one line per integration so you can confirm what is actually enabled.

### 6.0 At a glance

| Area | Variables | Required | Without it |
|---|---|:---:|---|
| KIS live | `REAL_APP_KEY` `REAL_APP_SECRET` `REAL_ACC_NUM` | mode 2 | Live trading unavailable |
| KIS auto-trading account | `AUTO_APP_KEY` `AUTO_APP_SECRET` `AUTO_ACC_NUM` | optional | Shares the live key |
| KIS paper mode | `VIRT_APP_KEY` `VIRT_APP_SECRET` `VIRT_ACC_NUM` | mode 1 | Paper mode unavailable |
| KIS fill notifications (WS) | `REAL_HTS_ID` (or `KIS_HTS_ID`/`HTS_ID`) | optional | Fill detection falls back to REST polling |
| Toss Securities | `TOSS_APP_KEY` `TOSS_APP_SECRET` `TOSS_ACC_NUM` | mode 3 | Toss mode unavailable |
| Telegram | `TELEGRAM_BOT_TOKEN` `TELEGRAM_CHAT_ID` `TELEGRAM_INSTANCE_NAME` | recommended | No alerts or remote control |
| Google Gemini | `GEMINI_API_KEY` `GEMINI_MODEL` `GEMINI_FALLBACK_MODEL` | optional | All AI features disabled |
| OpenDART | `DART_API_KEY` | optional | Domestic disclosure/dividend/financial features disabled |
| FRED | `FRED_API_KEY` | optional | Only US indicator dates are missing |
| TradingView | `TV_USERNAME` `TV_PASSWORD` | optional | Anonymous mode (lower quota and stability) |
| KRX Data System | `KRX_ID` `KRX_PW` | optional | Gold, indices, and historical flows fall back to alternate sources |
| Trading journal sync | `JOURNAL_API_URL` `JOURNAL_API_KEY` and others | optional | Journal sync disabled |

---

### 6.1 Korea Investment & Securities (KIS)

**How to obtain**
1. Open an account (e.g. via the mobile app)
2. Apply for the **KIS Developers** service
3. Issue the App Key / App Secret under **My Page > My Services > Issue**
4. Issue a **second key** for paper mode (mode 1) — TPS, WebSocket, and token-issuance limits are
   all per app key, so sharing one key makes the two instances eat each other's budget
5. (Optional) Note your **KIS HTS login ID** if you want real-time fill notifications over WebSocket

```sh
export REAL_APP_KEY="..."   ; export REAL_APP_SECRET="..." ; export REAL_ACC_NUM="12345678-01"
export VIRT_APP_KEY="..."   ; export VIRT_APP_SECRET="..." ; export VIRT_ACC_NUM="12345678-01"
export REAL_HTS_ID="myhtsid"                 # optional — subscription key for fill notifications
```

- **`AUTO_*` (optional)** — for running auto-trading on a separate account and app key. KIS limits (20 TPS, one concurrent WebSocket, token issuance) are **all per app key**, so splitting manual lookups from auto-trading keeps them from consuming each other's budget.
- **`VIRT_*` (mode 1)** — a dedicated key for paper mode, so observation never interferes with the live instance's order path. `VIRT_ACC_NUM` is **display-only** (it identifies the instance in alert footers); the internal account number is always `PAPER` as a safeguard.
- **`*_HTS_ID` (optional)** — the subscription key for fill notifications (H0STCNI0) is the HTS login ID. Without it, fill detection falls back to REST polling.
- Account numbers use the `12345678-01` form (8-digit account + 2-digit product code).

### 6.2 Toss Securities

**How to obtain**
1. Open a Toss Securities account in the **Toss app**
2. Apply for Open API access in the **Toss Securities developer center**
3. Issue the **App Key / App Secret**
4. **Register your allowed IP (required)** — see the warning below

```sh
export TOSS_APP_KEY="..." ; export TOSS_APP_SECRET="..."
export TOSS_ACC_NUM="..."     # optional — the first account is selected if omitted
```

> ⚠️ **Toss requires IP allow-listing.** From an unregistered network (mobile tethering, VPN, dynamic IP) token issuance itself is refused with `IP address not allowed`. Register the **public IP** of the machine that will run the program (developer center → app settings → allowed IPs); a static IP line is recommended for always-on operation.
> KIS does not require IP registration, but if you set a customer-IP restriction on the key, only that IP will connect. On token failure the pre-flight check prints your **current public IP along with cause-specific guidance**.

### 6.3 Telegram Bot

**How to obtain**
1. Search for **@BotFather** in Telegram and start a chat
2. Send `/newbot` and follow the prompts to name the bot
3. Copy the issued **API token**
4. Send any message (e.g. `start`) to your new bot
5. Run `python tools/get_telegram_chat_id.py`, paste the token, and it prints your **Chat ID**

```sh
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
export TELEGRAM_CHAT_ID="123456789"
export TELEGRAM_INSTANCE_NAME="RasPi3B"   # optional — alert prefix for multi-host setups
```

### 6.4 Google Gemini (AI features)

**How to obtain**
1. Open [Google AI Studio](https://aistudio.google.com/) and sign in with a Google account
2. **Get API key** → **Create API key**
3. The key runs on the **free tier** by default (no charge within the daily request quota)

```sh
export GEMINI_API_KEY="AIza..."
export GEMINI_MODEL="gemini-flash-latest"               # optional (default)
export GEMINI_FALLBACK_MODEL="gemini-flash-lite-latest" # optional — used on 429 (quota exceeded)
```

> The free tier's per-model daily quota is easy to hit. **Do not leave the fallback model empty** — on quota (429) or overload (503) the system retries automatically on the fallback.

### 6.5 OpenDART (disclosures)

Powers domestic **disclosure monitoring, the dividend/earnings calendar, supply/overhang signals, and financial snapshots**. Without it only those features are disabled; overseas dividends and earnings still come from yfinance without a key.

**How to obtain**
1. Go to [OpenDART](https://opendart.fss.or.kr) → **Apply/Manage Auth Key > Open API application** (top right)
2. Fill in basic details such as email and submit (issued instantly, free)
3. Retrieve the **40-character key** from the email (or under *Apply/Manage Auth Key > Open API key*)

```sh
export DART_API_KEY="40_character_key"
```

> The daily quota is 20,000 calls; the program only queries watchlist symbols once a day, well inside the limit.
> On first run it downloads the ticker↔DART corp-code mapping and caches it in `json/dart_corp_map.json` (auto-refreshed monthly).

### 6.6 FRED (US economic calendar)

Needed only for **US indicator release dates** (CPI, employment, PCE, PPI, GDP, retail sales, JOLTS) at the top of `[6] → 5. Investment Calendar`. FOMC dates come from the Federal Reserve's own calendar, and KR/US quadruple-witching dates are computed locally, so both **work without a key**.

**How to obtain**
1. Go to [FRED API Keys](https://fredaccount.stlouisfed.org/apikeys) → **Create New Account** if needed (free, email verification)
2. After signing in, click **Request API Key**, describe your use briefly, and submit (issued instantly)
3. Copy the **32-character key**

```sh
export FRED_API_KEY="32_character_key"
```

> FRED publishes scheduled future release dates, so the dates shown are **official, not estimated** (US local time). Results are cached daily in `json/econ_calendar_cache.json`. Events that are not machine-readable (e.g. Bank of Korea rate decisions) can be entered by hand in `json/econ_calendar_seed.json`.

### 6.7 TradingView (index & Treasury stability)

Not a key issuance — these are **your existing TradingView account credentials**. With them, tvDatafeed (used for indices and US Treasury yields) runs in logged-in mode with a better quota and stability than anonymous access.

```sh
export TV_USERNAME="tv_id"
export TV_PASSWORD="tv_password"
```

- Without them it runs anonymously and logs a WARNING (INFO on successful login).
- The token is cached for **7 days** in `data/tv_token.json` so restarts do not re-login — frequent logins trigger TradingView **CAPTCHA**. If a CAPTCHA appears, retry later or sign in once in a browser and restart.

### 6.8 KRX Data System (`KRX_ID` / `KRX_PW`)

Not an API key — these are the **web login ID and password for a [data.krx.co.kr](https://data.krx.co.kr) member account**. Sign up for free on the site and use those credentials.

```sh
export KRX_ID="krx_id"
export KRX_PW="krx_password"
```

> ⚠️ **This is a password — use a dedicated account you do not reuse elsewhere.** A restart is required; a process already running will not receive these values.

What these credentials switch on — without them everything **falls back silently**, so behavior is the same but data quality differs.

| Item | With KRX | Difference vs fallback |
|---|---|---|
| Historical investor flows | Range queries | The KIS flow API returns **only the last 30 trading days**, so multi-year backtests run with "smart money" switched off outside that window |
| KRX spot gold | Real OHLC + volume | Naver provides **close only** → bars must be flattened (distorting ATR/ADX) and OBV is impossible without volume |
| KOSPI200 / KOSDAQ150 daily | Settled bars + volume | tvDatafeed returns intermittent empty responses and zero index volume. **KOSDAQ150 has no ticker on Yahoo or FDR**, making tvDatafeed a single point of failure |
| Listed-symbol master | Names and market caps | Used to validate ticker codes in AI output (KONEX is filled in from FDR) |

> KRX only publishes **settled bars after the close**, so intraday prices still come from the existing real-time sources.
> **VKOSPI200 and KOSPI200 futures are mode-2 (KIS live) only.** Neither has an alternative real-time source, and filling the gap with settled bars leaves a stale number for the whole session — futures sessions cover nearly the whole day (day 09:00–15:45, night 18:00–06:00), drifting up to a full day (measured 40-point divergence during a night session), and a volatility index goes quiet precisely when volatility spikes. Rather than display an inaccurate number, both are omitted from the index list in Toss mode.

### 6.9 Trading journal web sync

Sends fills to a remote trading-journal server ([stock-memo](https://github.com/batmi/stock-memo)). The protocol follows [`UniversalTradingHistoryAPI.json`](UniversalTradingHistoryAPI.json) (OpenAPI 3.1), shared by both projects.

1. **Toggle**: `[0] → 5. Environment & System → 3. Data & Communication → Trading journal sync` (OFF by default)
2. **API key**: web dashboard → **Settings → HTS integration API key**

```sh
export JOURNAL_API_URL="https://memo.example.com"   # required (HTTPS recommended)
export JOURNAL_API_KEY="skm_..."                    # required
export JOURNAL_SOURCE="my-stock-hts"                # optional — must differ per installation
export JOURNAL_BOT_ID=""                            # optional (defaults to JOURNAL_SOURCE)
export JOURNAL_BOT_LABEL=""                         # optional — display name on the web
```

> The toggle **and** both URL and key must be present; the menu tells you what is missing.
> Running several hosts? `JOURNAL_SOURCE` must differ per machine, or the backfill checkpoints overwrite each other and leave gaps.

---

## 7. Project Structure

```text
my-stock-hts/
├── main.py                 # Entry point — menus and routing
├── config.py               # Single source of settings (defaults, env vars, dynamic validation)
├── run.sh / run.bat        # Launchers (auto-install dependencies)
├── requirements.txt        # Single source of runtime dependencies — read by run.sh
├── requirements-dev.txt    # Development/test dependencies (pytest)
│
├── core/                   # Lowest layer — code that knows nothing about trading
│   ├── constants.py        #   TR IDs, field mappings
│   ├── indicators.py       #   Indicator math (RSI, MACD, ADX, ATR, SAR…)
│   ├── utils.py            #   Dates, formatting, shared helpers
│   ├── jsonio.py           #   JSON load/save helpers
│   ├── caching.py          #   TTL cache (item caps, auto eviction)
│   ├── executors.py        #   Global thread pools (AI, IO, Telegram)
│   ├── trading_cost.py     #   Single source for fees and taxes
│   ├── session.py          #   Session, token, and mode management
│   └── context.py          #   Thread-global state and locks
│
├── brokers/                # Raw broker clients
│   ├── toss_api.py         #   Toss Securities Open API
│   └── realtime.py         #   KIS WebSocket quotes and fill notifications
│
├── api/                    # Quote/order API layer (callers use api.func())
│   ├── auth.py             #   Token issuance/refresh, shared call entry point
│   ├── http.py             #   TPS gate, retries, connection pool
│   ├── quotes/             #   Prices, order book, flows, overseas detail / NXT multi-quote
│   ├── charts.py           #   Daily, weekly, intraday bars
│   ├── chart_cache.py      #   Chart memory/disk cache, watchlist warm-up
│   ├── indices.py          #   Indices and K200 futures
│   ├── yf_quotes.py        #   yfinance / TradingView quotes
│   ├── sessions.py         #   Session detection (regular, pre, after, day market)
│   ├── market_calendar.py  #   Holidays and overseas clocks
│   ├── instruments.py      #   NXT eligibility, ETF/ETN detection
│   ├── account.py          #   Balances, fills, open orders
│   ├── orders.py           #   Place, modify, cancel, deposits
│   └── toss.py             #   Toss layer + domestic daily-bar fallback
│
├── modules/                # Feature modules (mapped to menu numbers)
│   ├── settings.py         # [0] Settings
│   ├── market.py           # [1] Market indices
│   ├── analysis.py         # [2] Quotes and technical analysis
│   ├── chart.py            # [3] Chart rendering
│   ├── web_dashboard.py    #     Responsive web dashboard for chart galleries
│   ├── backtest.py         # [4] Single-symbol backtesting
│   ├── portfolio_backtest.py #   N-slot portfolio backtest (slot competition, cash, heat cap)
│   ├── auto_trade/         # [5] System trading
│   │   ├── engine.py       #     Trading engine (strategy, orders, risk)
│   │   ├── trader.py       #     Main loop (analyze → buy/sell → report)
│   │   ├── conclusion.py   #     Fill monitoring and confirmation
│   │   ├── common.py       #     Shared helpers (restrictions, hours, order status)
│   │   └── menu.py         #     Trading-rule and restriction menus
│   ├── theme_analysis.py   # [6] Discovery + AI (Gemini) reports
│   ├── manage/             # [7] Watchlist + [6-5~8] fundamentals
│   │   ├── watchlist.py    #     Add/remove/view watchlist
│   │   ├── discover.py     #     [7-4] Candidate discovery
│   │   ├── events.py       #     [6-5] Dividend/earnings calendar
│   │   ├── econ_events.py  #     Key economic events (FRED, Fed)
│   │   ├── disclosure.py   #     [6-6] Disclosure monitoring
│   │   ├── insider.py      #     [6-7] Supply and overhang signals
│   │   └── financials.py   #     [6-8] Financial snapshot
│   ├── trading.py          # [8] Order management
│   ├── account.py          # [9] Assets and balances
│   ├── paper_broker.py     # Paper-mode virtual broker (intercepts at the api layer)
│   ├── paper_report.py     # [9-6] Paper account reporting
│   ├── reserved_order_monitor.py # Reserved-order watcher thread
│   ├── krx_daily.py        # Domestic daily bars (pykrx/FDR, KRX regular session)
│   ├── krx_data.py         # Official KRX data (gold, indices, derivatives, flows)
│   ├── intraday_bars.py    # Intraday bar collection/cache (tvDatafeed)
│   ├── dart_api.py         # OpenDART integration
│   ├── market_halt.py      # Circuit breaker / VI detection
│   ├── telegram_bot.py     # Telegram command handling
│   ├── telegram_notify.py  # Telegram sending
│   ├── scheduler.py        # Background scheduler
│   ├── heartbeat.py        # Process liveness stamps
│   ├── instance_lock.py    # Duplicate-instance guard (per app key)
│   ├── db_manager.py       # DB connections and queries
│   ├── db_queue.py         # Single-worker queue proxy (blocks SQLite locks)
│   ├── holdings_backfill.py# Rebuild trade history from broker fills
│   ├── journal_sync.py     # Trading journal sync (outbox)
│   └── prompts.py          # AI prompt templates
│
├── tools/                  # Diagnostics and utilities (109 files)
│   ├── web_server.py       #   Lightweight Flask web server for chart dashboard
│   ├── hts_watchdog.py     #   Process watchdog (cron) — alerts only, no restarts
│   ├── get_telegram_chat_id.py  # Verify Telegram Chat ID helper
│   ├── update_holidays.sh  #   Periodic holiday-library refresh
│   ├── journal_sync_e2e.py #   End-to-end journal sync verification
│   ├── check_*.py          #   Quote, fill, balance, and API diagnostics
│   ├── audit_common.py     #   Shared audit contract (exit sample, metrics)
│   └── audit_*.py          #   Strategy-dial backtest audits (~80 scripts)
│
├── tests/                  # pytest unit and integration tests (3,400+)
│
├── json/                   # [auto] settings and state
│   ├── stock.json                 # Watchlist
│   ├── restricted_stocks.json     # Restricted symbols
│   ├── dynamic_config.json        # Live baseline settings
│   ├── dynamic_config.{sim,toss,paper}.json  # Per-mode profiles (deltas only)
│   ├── daily_asset_state.json     # Start-of-day equity (daily loss limit)
│   ├── token_cache.json           # API token cache
│   └── dart_corp_map.json         # DART corp-code mapping cache
├── db/                     # [auto] SQLite (trades, reserved orders, signal ledger)
├── logs/                   # [auto] logs and heartbeat.json
├── chart/                  # [auto] chart images
└── data/                   # [auto] Excel/CSV exports, intraday cache
```

**Layering rule**: dependencies flow only `core` → `brokers` → `api` → `modules`. Lower layers never import upper layers at module top level (deferred imports inside functions are allowed), and `tests/test_layer_packages.py` enforces this.

---

## 8. Architecture & Stability

**Concurrency**
- **DB queue proxy** — every DB write is routed through a single worker queue, eliminating `database is locked` across threads.
- **Global thread pools** — AI, IO, and Telegram pools are reused instead of spawning threads ad hoc.
- **Thread-safe dynamic settings** — runtime changes take an `RLock`, and **Pydantic** validates types and ranges.

**Order safety**
- **Order state machine** — tracks accept → fill → cancel → partial fill to prevent duplicate orders and ghost positions. A lost order response is never retransmitted; it is **reconciled against the day's order history** instead.
- **Kill switch** — consecutive errors move the system to standby to protect the account.
- **Single-instance guard** — warns at startup if another process holds the same app key (TPS, WebSocket, and token limits are per app key).

**Quote consistency**
- **Unified real-time price** — the analysis screen and the auto-trader compute indicators from the same intraday price through a single entry point (`indicators.apply_realtime_price`).
- **WebSocket quotes and fill notifications** — KIS WS pushes prices and volume strength (automatic REST fallback when unsubscribed or disconnected), and fill notifications (AES256-CBC) wake fill confirmation immediately. A single connection caps at 41 subscriptions, so **holdings then buy candidates are always subscribed** and the remaining slots rotate. `USE_WEBSOCKET` applies without a restart.
- **Backtest/live data parity** — domestic backtests always use official KRX data (pykrx/FDR) regardless of mode, and a **warning** is raised when the retrieved window is shorter than requested, so truncation is never silent.

**Observability & operations**
- **Signal ledger** — live-only gates that daily bars cannot reproduce (volume strength, ask/bid ratio, same-day re-entry block) record their verdicts in the DB, **one row per (date, symbol)**, retained for 3 years by default. "Blocked all day" is distinguishable from "blocked on some cycles". Auto-trading logs are kept 120 days, separate from general logs (30 days). Analyze with `python3 tools/audit_signal_ledger.py`.
- **Process death detection** — the live process stamps `logs/heartbeat.json` every minute together with a deadline for its next stamp; an external cron watcher (`tools/hts_watchdog.py`) only checks whether that promise expired. **It never auto-restarts** — restarting blind either dies the same way or comes back half-alive and places orders. Clean shutdowns do not alert.
- **Per-mode config profiles** — live reads and writes only the baseline file; other modes layer their own profile on top, storing only the deltas. A safeguard disabled for observation cannot leak into live, and promotion is manual.
- **Memory protection** — quote and chart caches have item caps with oldest-first eviction, so memory does not grow without bound on a 1 GB Raspberry Pi.

**API efficiency (TPS)**
- **Adaptive TPS (AIMD)** — raises the effective rate as successes accumulate and backs off immediately on `EGW00201`, converging on a sustainable value.
- **NXT time gating** — skips NXT lookups during the regular session, roughly halving per-symbol calls.
- **Persistent daily-bar cache** — kept per trading day on disk, so restarts do not re-fetch the same day.
- **Skips unneeded order-book calls**, and **worker count is matched to TPS**.

---

## 9. Telegram Bot

| Category | Commands |
|---|---|
| System control | `/start` `/stop` `/restart` `/status` `/health` `/config` |
| Account & assets | `/balance` `/holdings` `/pending` `/reserves` `/profit [period]` `/history [period]` `/report [period]` `/stats [symbol]` |
| Market & analysis | `/market [group]` `/signal <symbol>` `/analyze <symbol>` `/chart [period] <symbol>` `/briefing` `/closing` `/curate` `/scan [market]` `/news <symbol>` `/calendar [days]` `/ask <question>` |
| Management | `/stocks` `/rules [symbol]` `/restrict` `/addrestrict <symbol> [reason]` `/delrestrict <symbol>` `/memo [a/d/symbol]` `/log` `/help` |

> Commands keep evolving. **Send `/help` to the bot for the current list.**

---

## 10. Reserved Orders

Beyond a broker's simple price/time reservations, orders can trigger on **quant scores and technical indicators**. A background thread checks every 3 seconds, and reservations persist in SQLite across restarts.

| Type | Trigger |
|---|---|
| STOP | Price falls to or below the target (take-profit or stop-loss sell) |
| BREAKOUT | Price breaks above resistance (momentum buy) |
| LIMIT | Limit price touched |
| TIME | A specified time (e.g. 15:20) |
| SCORE | Quant score reaches or falls below a threshold |
| RSI | RSI crosses a threshold |
| TRAILING_BUY | Rebound of N% off the low (bottom fishing) |
| TRAILING_SELL | Decline of N% from the high (profit protection) |
| EMA | Price crosses an EMA up or down |

**Protections**
- **Validity period** (today / this week / this month / open-ended) with automatic expiry
- **Duplicate protection** — selling out a symbol cancels its pending reserved sells; a new buy cancels stale reserved buys
- **Network savings** — quote lookups are blocked after hours (20:00–08:00) and during single-price auctions (08:50–09:00, 15:20–15:30)
- **Market-price substitution** — set the execution price to `0` and the order is sent at the price prevailing when it triggers

### How journal sync works

The fill path never touches the network. `insert_trade()` writes to `journal_outbox` in the **same transaction** as the trade record, and a background worker sends batches every 30 seconds (exponential backoff on failure). The server deduplicates by idempotency key, so retransmission is always safe.

| Failure | Covered by | Recovery |
|---|---|---|
| Server down / network cut | Stage 1 queue | Sent automatically on recovery |
| Sync toggled off / env vars missing | Stage 2 backfill | Local `trades` reconciled against the server's last sync point (once 60s after startup, then every 6 hours) |

- **What is sent**: `filled` (CONFIRMED) and `filled (estimated)` (ESTIMATED) only — `accepted` and `cancelled` are not.
- **Paper mode (mode 1)**: uses the same toggle. Fills go out flagged `isSimulated=true`, kept apart from live records and statistics on the server, and the bot id splits off as `…:paper:PAPER` so a paper bot never overwrites the live bot's status slot. The toggle is stored per mode (`dynamic_config.paper.json`), so turning it on in paper mode does not leak into live.
- **`isSystem` classification**: only AutoTrader orders tagged `(AUTO)` are `true`. Reserved orders execute unattended but **a human set the condition**, so they are not counted as system trades.
- **Records deleted on the web are not resent** automatically (otherwise an intentional deletion could never stick). Use **Account settings → Resync** on the web. **A restore from backup always requires a resync.**
- Verify with `python tools/journal_sync_e2e.py [--cleanup]` — a synthetic fill travels the production path: enqueue → send → read back → field comparison.

---

## License

Released under the [Apache License 2.0](LICENSE.md).
