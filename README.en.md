# MyStock HTS (Home Trading System)

[Korean](README.md) | [English](README.en.md)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

A Python-based personal stock auto-trading and analysis system utilizing the Korea Investment & Securities (KIS) and Toss Securities Open APIs.
It operates in a terminal (Console) environment and provides real-time market quotes, precise technical analysis, and strategy-based auto-trading features.

> **The fundamental principle of this trading system is Trend Following.** Losses are cut fast and profits run until the trend breaks (no fixed take profit; the Chandelier trailing stop is the primary exit), and only strong, durable trends are bought. Settings that violate this principle (fixed take profit, half take-profit, RSI overheating sell, defensive half sell, mean-reversion buying) are preserved in code but disabled by default and hidden from the settings menus.

## Disclaimer

> **[IMPORTANT] Please read this before using the software.**

1. **Responsibility for Investment Results**: This software was written for development and educational purposes, and it is merely a supplementary tool for investment. All investment losses and damages incurred by using this software are entirely the **user's responsibility**, and the developer assumes no legal or financial liability.
2. **System Risks**: Due to API server failures, network instability, program bugs, or logic errors, orders may be omitted, duplicated, or executed at unintended prices. Users must continuously monitor the system and, in case of emergency, immediately halt the program and respond directly via the broker's HTS/MTS.
3. **Verification Recommended**: Before real trading, please thoroughly verify the system's stability and the validity of your strategy in a **mock investment (simulation)** environment for a sufficient period.

---

## Table of Contents
1. [Overview & Objective](#1-overview--objective)
2. [Trading Strategy](#2-trading-strategy)
3. [Configuration](#3-configuration)
4. [Architecture & Stability](#4-architecture--stability)
5. [Project Structure](#5-project-structure)
6. [Prerequisites](#6-prerequisites)
7. [Installation & Execution](#7-installation--execution)
8. [Telegram Bot](#8-telegram-bot)
9. [Disclosure Integration (OpenDART)](#9-disclosure-integration)
10. [AI-Powered Assistant](#10-ai-powered-assistant)
11. [Reserved Order System](#11-reserved-order-system)
12. [Known Issues](#12-known-issues)
13. [Trading Journal Sync](#13--trading-journal-sync)
14. [License](#14-license)

## 1. Overview & Objective

This program is an all-in-one stock tool based on a CLI (Command Line Interface) designed for **Quant and System Traders who value technical analysis**.
Without the need for a heavy HTS (Home Trading System), you can quickly and intuitively check real-time quotes, balances, and execute orders in a terminal environment. It helps you catch trading timing based on **data and indicators (EMA alignment, RSI, MACD, etc.)** rather than relying on intuition.

### Key Features
*   **HTS/MTS Replacement:** Real-time quote inquiry and buy/sell/modify/cancel order execution from the terminal.
*   **Full Support for NXT (Alternative Trading System) & SOR (Smart Order Routing):** Fully supports real-time quote integration and auto/manual/reserved trading during the regular session (KRX) as well as the Nextrade (NXT) operating hours (08:00~08:50, 15:30~20:00). (Note: Mock trading does not support this due to KIS API specs.)
    *   That said, **system trading runs on the KRX regular session (09:00–15:30) by default**, since every indicator and daily bar is KRX regular-session based (pykrx/FDR). To let the auto-trader work the NXT extended sessions too, widen start/end to `0800`/`2000` under `[0] → 5-1. Trading Hours & Cycle`.
*   **Enterprise-grade Reserved Orders:** Surpasses HTS limitations by supporting 24-hour background reserved trading based on quant scores, RSI, and trailing stops.
*   **Technical Analysis Automation:** Automates complex supplementary indicator calculations to provide intuitive investment judgment signals such as **'Buy/Wait/Rise/Interest/Watch/Caution/Sell'**.
*   **Individual Stock Strategy Settings:** Allows setting different buy/sell criteria (score, RSI) and take-profit/stop-loss/trailing-stop ratios individually per stock.
*   **In-depth Index Analysis:** Provides detailed charts for market indices such as KOSPI and NASDAQ, along with AI in-depth reports combined with macro environments.
*   **AI Investment Assistant:** Utilizes Google Gemini LLM to provide in-depth stock diagnostics, analysis of market-leading themes, interactive Q&A, and pre-market briefings.
*   **DART (Electronic Disclosure) Integration:** Utilizes OpenDART API for watchlist **disclosure monitoring** (importance classification + AI good/bad news interpretation + auto-extracted details for supply contracts, treasury stock, bonus issues, etc.), **dividend/earnings calendar** (confirmed record-date parsing, estimated earnings dates), **supply-demand & overhang signals** (treasury stock decisions, mezzanine overhang, insider/5% reports), **financial snapshot** (standalone quarterly earnings, ROE, debt ratio), and **real-time Telegram alerts for major disclosures**.
*   **Market Index Filtering:** Risk management feature that analyzes the trend of KOSPI/KOSDAQ indices and automatically suspends buying in a downtrend.
*   **Real-time Market Halt Alerts (Circuit Breaker / VI):** Detects market-wide circuit breakers (CB) and per-stock Volatility Interruptions (VI) based on actual exchange status flags and instantly notifies via Telegram. (CB on by default; VI is optional.)
*   **Portfolio De-synchronization (Correlation Filtering):** Prevents duplicate purchases of stocks that have similar price movements (correlation coefficient of 0.7 or higher) to currently held stocks, inducing portfolio diversification.
*   **Real-time Configuration Changes:** The ability to immediately change and permanently apply buy/sell conditions and investment weights while system trading is running.
*   **Trading Restricted Stock Management:** Exclude specific stocks from system trading (buy/sell restricted) and set to only receive alerts when buy signals occur.
*   **Integrated Management for Domestic/Overseas:** Integrates management of domestic and US stocks into a single interface using the KIS API.
*   **Support for Real/Mock Trading:** Easily switch between mock investment and real investment by just changing settings.
*   **Strategy Backtesting:** Validate the current trading strategy based on historical data and simulate expected returns.
*   **Enterprise-grade Stability & Performance Optimization:** 
    *   Strict dynamic configuration validation using **Pydantic** and a Thread-safe architecture.
    *   **Global Thread Pool reuse mechanism** preventing API bottlenecks and OS resource waste, driven by an independent background scheduler.
    *   Introduces a DB proxy architecture based on a worker Queue to **fundamentally block SQLite Lock issues** in a multi-threaded environment.

## 2. Trading Strategy

> **Notice:** System trading (auto-trading) only operates on the **'Domestic Stocks (국내주식)' list** registered in the 'Interest Stock Management' menu.

This system is based on a **Trend Following** strategy, catching high-probability entry points through multi-faceted technical analysis and executing strict risk management.

> **Trend-Following Doctrine (Core Principles)**: ① Cut losses short, let profits run — there is no fixed take-profit ceiling; the **trailing stop (Chandelier Exit)** is the primary exit. ② Buy strong, durable trends — buy candidates are prioritized by **composite score** → trend quality (regression momentum) → 52-week-high proximity → volume strength, and trend strength is measured by the **price momentum (absolute momentum)** scoring factor. Only positions validated by profit are **pyramided (scaled up)**; averaging down into losers is structurally impossible. ③ The trend is the market — new buys are suspended when the index is below its reference moving average.

> This section explains the **concepts** of the strategy. For the trigger thresholds (defaults) and parameter details of each condition, see **[3. Configuration](#3-configuration)**. (All values are configurable.)

### 1. Buy Strategy
Buying is executed when both the composite score calculated through the **Quant Multi-Factor Model** and the filtering conditions are satisfied.

*   **Entry Conditions (AND condition)**:
    1.  **Composite Score**: At or above the buy threshold (`BUY_SCORE`) — see [3. Scoring System](#3-scoring-system) below for how the score is built
    2.  **Overheating Prevention**: RSI under the allowed ceiling (`BUY_RSI_MAX`) (relaxed when Super Momentum triggers)
    3.  **Supply & Demand Check**: Volume strength at or above the threshold (`BUY_VOL_STRENGTH`) (buying pressure dominance)
    4.  **Market Filter**: KOSPI/KOSDAQ index above its reference moving average (`MARKET_FILTER_MA`, default 80 days) or still inside the break band (`MARKET_FILTER_BAND`, default 1%) (avoiding downtrends)
    5.  **Overheated-Trend Block**: Trend quality below the ceiling (`TREND_QUALITY_MAX`, default 300; 0 disables it)
        *   Higher trend quality is **not** uniformly better. Over ten years of measurement the peak sits around 100–140, and above 300 the 20-day forward return turns negative (100–300 +2.74% → 300–1k -5.69%) while the tail is cut off (top-10% 56.6 → 14.2). This is a **momentum crash** at the single-stock level: right after a violent run-up, yesterday's winners collapse hardest.
        *   Adopted 2026-08-18. Across two seed sets (36 and 75 paired runs) with 20% delisted names mixed in, MDD improved in **all eight windows** (nearly halving in the crash window, -36.7% → -21.9%), and a random control blocking the same fraction of entries was indistinguishable from the baseline — confirming the gain comes from *reading trend quality*, not from simply buying less. Names with fewer than 90 bars of history pass through (fail-open).

*   **Buy Priority**: When multiple candidates pass the entry conditions (the gate), they are bought in order of **① composite score → ② trend quality → ③ 52-week-high proximity → ④ volume strength**.
    *   **Trend Quality (Regression Momentum)**: The **annualized slope × R²** of a linear regression over the last 90 days (`TREND_QUALITY_LOOKBACK`) of log closes (Clenow momentum). The slope measures trend strength and R² measures smoothness (a proxy for persistence). The score is a sum of binary signals and ties are frequent (25–32% of days with slot competition), so the continuous trend-quality value breaks those ties.
    *   **Why the score ranks first (changed 2026-08-12)**: Trend quality used to be the primary key, but a 10-year measurement (15 runs × 25 symbols, paired comparison) showed it losing in all four sub-periods (19/60; 10-year return 384.9% → 286.8%, top-10% trade in the high-volatility window 141.8 → 62.8). The cause: trend quality is continuous, so it never ties — meaning the score, its secondary key, **never once** decided a ranking. Putting the score first and using trend quality to break its ties cleared the adoption bar at 33/60 across the four sub-periods.

*   **Leading Stock Following: Super Momentum**:
    *   For leading stocks making strong rallies near 52-week highs, the standard overheating criteria (allowed buy/sell RSI) are relaxed to follow the market's strong trend to the end.

*   **Pyramiding: Scaling into Winners**:
    *   For held positions whose return is at or above the trigger (`PYRAMIDING_PROFIT_TRIGGER`, default +10%) with the buy signal (trend) still intact, the position is increased by a ratio of the held quantity (`PYRAMIDING_RATIO`, default 50%), limited to once per position by default.
    *   The exact opposite of averaging down — **it never fires on losing positions.** The stop-loss rate for the added tranche is recalculated from the ATR at the time of scaling and feeds into the weighted-average stop.

*   **Downtrend Exclusive: Oversold Mean Reversion** — **disabled by default (hidden from menus)**:
    *   A strategy aiming for a technical rebound in oversold zones. As a counter-trend edge that conflicts with the trend-following doctrine (it needs short reversion targets and tight stops, mismatching the system's wide trend-following exits) and competes for scarce position slots, it is **disabled in the defaults and in every preset and is not exposed in the settings menus**. (Re-enabling is only possible by editing `json/dynamic_config.json` directly.)

### 2. Sell Strategy
Following the trend-following doctrine — **"cut losses fast, keep the upside open until the trend breaks"** — the exit conditions are monitored in the following priority order, with no fixed take-profit ceiling.

1.  **Stop Loss**: Immediate sell when the loss rate reaches the limit. With `USE_ATR_STOP`, a dynamic stop based on the volatility (ATR) at purchase time is applied per stock.
    *   **Break Even Stop**: When the maximum return achieved reaches the trigger level, the stop-loss line is automatically raised to the break-even profit zone (+0.5%) so a winner cannot turn into a loser.
2.  **Time-based Stop**: Only stocks that are **still at a loss** after the set period and have lost upward momentum are liquidated. (Profitable positions are never sold on time; postponed while the uptrend holds.)
3.  **Trailing Stop — Primary Exit (Chandelier Exit)**: Arming is not a fixed percentage — **the stock's own volatility sets it** (breakeven-linked). Once armed, sell when the price drops from the peak by a dynamic callback. **Arming and the callback use different ATR multipliers** — when one value did both jobs, arming earlier also meant tightening the exit, which destroyed the upside. Volatile leaders get a proportionally wider callback so the trend can be followed to the end. (The default `TS_MAX_GIVEBACK_RATIO=0` removes the giveback cap — a pure Chandelier; set it to a positive ratio to cap how much of the maximum gain can be given back.)
4.  **Trend Broken**: Full liquidation if the composite score drops below the sell threshold or the state is classified as 'Sell'.

> **Strategy presets retired (2026-07-20)**: The market-phase strategy presets (bull/bear/sideways) were retired after backtesting. Over 30 stocks × 3.8 years the bull preset was effectively identical to the defaults (17.15% vs. 17.48%), the bear preset cut returns to a third while halving >30% winners from 29 to 15 trades (PF 1.70→1.29), and the sideways preset made **zero trades in 3.8 years** because `BUY_RSI_MAX=50` logically contradicts a score threshold that requires RSI above 50, with super-momentum — the only escape hatch — also disabled. Phase handling is already automated through the market filter, regime/whipsaw risk scaling, drawdown step-down, and adaptive buy thresholds; having a human classify the regime and swap the whole strategy collides with that automation. (Settings menu 7 and the Telegram `/preset` command were removed.)

> **Disabled-by-default options (upside limiters, hidden from menus)**: Fixed take profit (`TAKE_PROFIT_RATE=0`), half take-profit (`HALF_TAKE_PROFIT_USE=False`), RSI overheating sell (`TAKE_PROFIT_RSI=0`), and defensive half sell (`DEFENSIVE_HALF_SELL_USE=False`) cut off the fat tail of profits, so they are turned off in the defaults and in every preset, and **are not exposed in the settings menus to protect the trend-following principle**. Re-enabling is only possible by editing `json/dynamic_config.json` directly or via per-stock custom rules (auto-trading menu).

### 3. Scoring System
The composite score determining whether to buy is calculated based on the **Quant Multi-Factor Model**. (Total 10 points, 0.5 point increments)
*The weight of each factor can be adjusted via settings (`SCORING_WEIGHTS`); below are default values.*

1.  **Trend Factor [Default 4.0]**
    *   **Moving Average**: Current > 20MA (+0.5), 20/60/120MA Alignment (+1.0), 5MA > 20MA (+0.5) — the highly correlated EMA signal cluster is **capped at 2.0 points** in total (prevents over-crediting)
    *   **Early Trend Reversal**: If 20MA <= 60MA, current price crosses above 60MA (+0.5)
    *   **Trend Persistence**: Close was above the 60MA for ≥ 70% (`TREND_PERSIST_MIN`) of the last 120 days (`TREND_PERSIST_LOOKBACK`) (+0.5) — a durability signal measuring "how long the trend has held" rather than the current snapshot, separating freshly crossed, unproven trends from long-sustained ones.
    *   **MACD**: MACD > Signal Golden Cross (+0.5), MACD Histogram positive or rising (+0.5)
    *   **SAR**: Current price > SAR (Uptrend +0.5)

2.  **Momentum Factor [Default 2.5]**
    *   **RSI**: RSI >= 50 Bullish (+0.5), RSI >= 60 Momentum expansion (+0.5), 40 <= RSI < 50 Upside potential (+0.5)
    *   **CCI**: CCI > 0 Uptrend (+0.5), CCI > -100 Escaping oversold (+0.5)
    *   **DMI**: +DI > -DI Cross (+0.5)
    *   **Price Momentum (Absolute Momentum)**: 6-month (`MOMENTUM_LOOKBACK`) return positive AND 52-week position ≥ 80% (`MOMENTUM_W52_NEAR`) — bonus for leaders near their highs (+0.5).
        *   **Multi-horizon Alignment Gate**: The bonus is withheld if the 1-month or 3-month (`MOMENTUM_LOOKBACK_1M/3M`) return is negative — blocking top-of-trend entries into **"cooling trends"** whose 6-month figure looks good but whose recent momentum has rolled over.

3.  **Strength & Volume Factor [Default 1.5]**
    *   **ADX**: ADX >= 20 Trend formation confirmed (+0.5)
    *   **Volume**: Volume explosion compared to 20-day average (200%+) & Bullish candle (+0.5)
    *   **OBV & Smart Money**: OBV rising & Major supply turnaround (+0.5)

4.  **Synergy Bonus [Default 2.0]**
    *   **Trend Start**: Current > 60MA AND MACD positive (or expanding) AND ADX >= 20 (+1.0)
    *   **Momentum Explosion**: MACD positive (or expanding) AND (RSI >= 60) AND (OBV rising) (+1.0)

#### Scoring Guide
*   **8.5 ~ 10.0 points (Strong Buy)**: All indicators point to an uptrend with perfect correlation. Good to enter with a high weight.
*   **7.0 ~ 8.5 points (Buy)**: The trend is clear, but some secondary indicators haven't followed yet. (Default buy threshold `BUY_SCORE` = 7.0) Good for split buying.
*   **Wait (score ≥ `BUY_SCORE`)**: The score already meets the buy threshold, but the short-term **RSI is overheated** (at or above `BUY_RSI_MAX`, yet below the overheat-caution line), so entry is merely deferred. This is a *"too strong, wait for a pullback"* **buy-the-dip standby** — the opposite of "Caution" (which signals weakness/danger). It is treated as a sibling of "Rise" (same color, time-stop grace, and screening visibility). It automatically flips to "Buy" once RSI cools. (If RSI climbs further to the overheat-caution line, it is demoted to "Caution".)
*   **6.0 ~ 7.0 points (Rise)**: The trend is aligned and alive, but the score falls slightly short of the buy threshold. (score-accumulation standby, `RISE_SCORE` = 6.0)
*   **Interest / Nascent (regardless of score)**: The trend alignment is **not yet complete**, but **early trend-reversal signals are detected in a minimum count or more** (`INTEREST_SIGNAL_MIN`) with no clear risk signals. Intended for **manual swing (short-term) trading monitoring** to quickly recognize whether it may develop into an actual buy stage; it is not an automatic-buy target. (See 3-2 for the seven early signals and detailed conditions.)
*   **Below 5.0 points (Sell/Avoid)**: Downtrend or sideways market with no clear direction.

## 3. Configuration

You can set the **Global Strategy** in the `config.py` file, and apply individual settings per stock via the **'System Trading > Per-Stock Trading Rules'** menu in the program.
Also, you can modify global settings in real-time during execution via the **'Main Menu > [0] System Settings'** menu (persists even upon restart).

### 1. Technical Indicator Settings (`INDICATOR_PARAMS`)
*   **Chart Lookback Days (`CHART_LOOKBACK_DAYS`)**: 730 days (2 years). Retrieves sufficient past data for accurate calculation of moving averages (e.g., 120-day line).
*   **RSI (Relative Strength Index)**:
    *   Period: 14 days (`RSI_PERIOD`)
    *   Overbought limit: 70 (`RSI_UPPER`) / Oversold limit: 30 (`RSI_LOWER`)
*   **Parabolic SAR**:
    *   Acceleration Factor (AF): Start 0.02 (`SAR_AF_START`), Step 0.02 (`SAR_AF_STEP`), Max 0.2 (`SAR_AF_MAX`)
    *   Increasing the value makes reversal signals faster, but may increase fake signals.
*   **CCI (Commodity Channel Index)**:
    *   Period: 20 days (`CCI_WINDOW`)
    *   Overbought/Oversold limit: ±100 (`CCI_UPPER`, `CCI_LOWER`)
*   **ADX (Average Directional Index)**: Period 14 days (`ADX_PERIOD`). A stable trend is considered formed when it's 20 or higher.

### 2. Buy/Analysis Thresholds (`ANALYSIS_THRESHOLDS`)
*   **Buy Score (`BUY_SCORE`)**: Default **7.0 points**. A buy signal is generated when the composite score combining various technical indicators is equal to or higher than this value.
*   **Rise Score (`RISE_SCORE`)**: Default **6 points**. It doesn't meet the buy criteria, but there is an upward flow.
*   **Interest Signal Minimum (`INTEREST_SIGNAL_MIN`)**: Default **3**. When trend alignment is incomplete but early trend-reversal signals — seven kinds: short-term golden cross, MACD improvement, +DI dominance, RSI crossing above 50, CCI improvement, supply inflow, MA60 proximity — are detected in this many counts or more with no risk signals (MACD dead cross, -DI dominance, RSI overheating/depletion, etc.), the stock is classified as **'Interest' (nascent)**. Detected even below the 120-day line, intended for manual swing-trade monitoring. (0 disables it.)
*   **Interest MA60 Proximity Ratio (`INTEREST_MA60_NEAR`)**: Default **0.97**. If the current price is at or above this ratio of the 60-day line (e.g., 97%), it counts as an 'MA60 breakout attempt' early signal even while still below the 60-day line.
*   **Maximum Buy Allowed RSI (`BUY_RSI_MAX`)**: Default **70**. Even if the buy score is met, we do not enter if the RSI is above this value, considering it already overheated.
*   **Buy Volume Strength (`BUY_VOL_STRENGTH`)**: Default **100.0%**. The volume strength at the time of purchase must be at least this value (buying pressure dominance).
*   **Mean Reversion (`USE_MEAN_REVERSION`)**: Catches the point where indicators rebound after reaching oversold in a downtrend or sudden drop. **(Disabled in defaults and all presets per the trend-following doctrine and hidden from the settings menus; re-enabling requires editing `json/dynamic_config.json` directly.)**
    *   `MR_RSI_MAX`: Maximum allowed RSI for mean reversion entry (Default 40.0)
    *   `MR_DISPARITY_MAX`: Disparity limit compared to 20-day MA (Default 90.0% or less)
    *   `MR_VOL_STRENGTH`: High volume strength to confirm buying pressure at the bottom (Default 120.0%)
*   **Super Momentum (`SUPER_MOMENTUM_USE`)**: Relaxes the buy/sell RSI thresholds for powerful leading stocks (new high rally) to follow the trend longer.
    *   `SUPER_MOMENTUM_SCORE`: Minimum trigger composite score (Default 8.0 points)
    *   `SUPER_MOMENTUM_W52_POS`: Minimum 52-week high position (Default 90.0% or higher)
    *   `SUPER_BUY_RSI_MAX`: Maximum allowed buy RSI relaxed upon trigger (Default 80.0)
*   **Pyramiding (`PYRAMIDING_USE`)**: Scales up only held positions validated by profit. (The opposite of averaging down — never fires on losing positions.)
    *   `PYRAMIDING_PROFIT_TRIGGER`: Minimum return to trigger scaling (Default +10.0%)
    *   `PYRAMIDING_RATIO`: Added quantity as a ratio of the held quantity (Default 0.5 = 50%)
    *   `PYRAMIDING_MAX_COUNT`: Maximum scale-ups per position (Default 3) — optimal under the 4-slot portfolio backtest; the smaller the seed, the larger the benefit

### 3. Sell Strategy (`SELL_STRATEGY`)
*   **Stop Loss**: Confirm loss when the loss rate reaches **-7.0%** (`STOP_LOSS_RATE`).
*   **ATR Stop Loss**: If `USE_ATR_STOP` is True (default), use ATR × `ATR_STOP_MULTIPLIER` (default 2.0) at the time of purchase as the stop loss rate instead of a fixed rate.
*   **Max ATR Stop Loss Rate**: `MAX_ATR_STOP_LOSS_RATE` is a safety mechanism to prevent the stop loss width from becoming abnormally large due to data errors or excessive volatility. (Default -15.0%)
*   **Break Even Stop**: When the highest return achieved reaches `BREAK_EVEN_PROFIT_RATE` (default 5.0%, dynamically linked when ATR is in use), raise the stop loss to `BREAK_EVEN_STOP_RATE` (default +0.5%) to defend profits.
*   **Time-based Stop**: If `TIME_STOP_USE` is True, sell when — after the set days (`TIME_STOP_DAYS`, default 15 days) — the return is below `TIME_STOP_MIN_PROFIT_RATE` (default **0.0%**, i.e., only positions still at a loss) and upward momentum has been lost. (Postponed if uptrend is maintained)
*   **Trailing Stop — Primary Exit (Chandelier Exit)**:
    *   **Trigger Condition**: `TS_ACTIVATION_MODE` (default `"breakeven"`) — **breakeven-linked**. The stop arms once the position can absorb one normal retracement (ATR × `TS_ACTIVATION_ATR_MULTIPLIER`, default 3.0): activation = cb ÷ (1 − cb) where cb = ATR × multiplier ÷ entry price. Because volatility sets the timing, low-volatility names arm near +10% while volatile ones arm near +40%. `TS_ACTIVATION_MAX_RATE` (default 0 = off) caps the activation level.
        *   **[Important] The activation multiplier is separate from the callback multiplier (3.5).** While one key did both, lowering it to arm earlier also narrowed the exit and the largest single trade collapsed from +165% to +75%. Holding the callback fixed and lowering only the activation preserves the fat tail.
        *   `TRAILING_STOP_ACTIVATION_RATE` (10.0%) is used only as the `"fixed"`-mode value or as a fallback when ATR is unavailable.
    *   **Sell Condition**: Effective callback = max(`TRAILING_ATR_MULTIPLIER` (default 3.5) × ATR ÷ peak, minimum `TRAILING_STOP_CALLBACK_RATE` (default 5.0%)). Volatile leaders get a proportionally wider callback to follow the trend longer. (The trailing ATR multiplier is separate from the stop-loss `ATR_STOP_MULTIPLIER`. `TS_MAX_GIVEBACK_RATIO` (default 0 = cap removed, pure Chandelier) can be set to a positive ratio to cap the giveback of the maximum gain. `TRAILING_STOP_CALLBACK_MAX` (default 0 = off) is an absolute cap on the callback itself, independent of MFE; measurements show tightening it only shaves the fat tail, so it ships disabled.)
*   **Trend Broken Sell**: Sell if the composite score falls below **5 points** (`SELL_SCORE`) or the state is classified as 'Sell'.
*   **Grace Period Stop Loss (`MR_GRACE_LOSS_RATE`)**: The maximum allowable loss rate during the grace period for stocks entered via mean reversion. (Default -7.0%; irrelevant while mean reversion is disabled)
*   **Disabled-by-default options (upside limiters — hidden from menus; enable only via direct `dynamic_config.json` edits or per-stock custom rules)**:
    *   **Take Profit**: `TAKE_PROFIT_RATE` = **0 (unused)**. If set, full liquidation at that return.
    *   **Half Take-Profit**: `HALF_TAKE_PROFIT_USE` = **False**. If enabled, 50% is pre-sold at half the take-profit target.
    *   **Overheating Sell**: `TAKE_PROFIT_RSI` = **0 (unused)**. If set, preemptive sell when RSI exceeds it. (`SUPER_TAKE_PROFIT_RSI` applies under Super Momentum.)
    *   **Defensive Half Sell**: `DEFENSIVE_HALF_SELL_USE` = **False**. If enabled, sell 50% on a downward reversal signal (SAR Sell + 5MA breakdown).

### 4. Risk Management & Filtering
*   **US Day-Market (Overnight Session) Quotes**:
    *   **Overview**: Reflects live prices from the US overnight ATS session (ET 20:00–04:00 = KST 09:00–17:00 during DST). KIS calls this "day market" and serves it under **different exchange codes** from the regular session (NAS→`BAQ`, NYS→`BAY`, AMS→`BAA`).
    *   **Behavior**: During the day-market session, day-market codes are tried first and fall back to regular codes when a symbol has no overnight prints. Outside the session, only regular codes are queried so no extra API calls are made.
    *   **Exchange cache**: Even when a day-market code returns the quote, `stock.json` and the exchange cache **always store the regular code** — a stored day-market code would break both regular-hours quotes and the order path.
    *   **Daily-bar attribution**: Overnight prints belong to the *next* trading session (a print at ET 21:00 belongs to the following day), so the market reference date follows the session it belongs to. Otherwise the overnight price would overwrite the already-settled prior regular bar and corrupt indicators.
    *   **Background**: The order path already recognized the day market (order division `31`), but the quote path queried only regular codes, so **the prior regular close stayed frozen for the whole session** (measured: MU `NAS` $970.82 +12.17% [frozen] vs `BAQ` $949.00 −2.25% [live]). Orders could be placed while prices could not be seen.
*   **Slippage Adjustment**:
    *   **Overview**: An adjustment to account for the difference between the price at the time of the order and the actual execution price (unfavorable execution).
    *   **Backtesting**: Conservatively reflects actual trading costs (spread, execution delays) by assuming a higher purchase price and a lower selling price.
    *   **Real/Auto Trading**: Adjusts the order price to a more favorable direction (Buy: +0.1%, Sell: -0.1%) when placing market orders to increase the execution probability in rapidly changing markets.
    *   **Settings**: Default `0.002` (0.2%). Can be changed in `config.py` or the settings menu.
    *   **Recommendation**:
        *   **Large Caps/ETFs**: 0.1% ~ 0.2% (abundant liquidity)
        *   **Mid/Small Caps**: 0.3% ~ 0.5%
        *   **High Volatility**: 0.5% ~ 1.0%

*   **Monte Carlo Simulation**:
    *   **Overview**: Adds noise and repeatedly tests to verify the robustness of a strategy.
    *   **Method**: Injects ±1% random noise into the price data, reflects uncertainties like slippage variation and execution failures (1%), and runs 1000 times.
    *   **Result**: Validates whether the strategy is based on luck or skill via average returns, worst case (VaR 95%), standard deviation, etc.

*   **Market Index Filtering**: Automatically suspends new buys when the KOSPI/KOSDAQ index breaks **more than 1% (`MARKET_FILTER_BAND`)** below its moving average (default **80 days**, `MARKET_FILTER_MA`), treating it as a 'downtrend'; buying resumes only once the index recovers by the same margin (+1%). **The same rule (using your current settings) is also modeled in backtests**, so new entries on index-weak days are blocked in simulations as well (live/backtest parity).
    *   **Period & band validation (2026-08-03, 60 days → 80 days + 1% band)**: Re-validated over 21 years of index data (2005–2026), 19,477 actual buy signals (2016–2026), and 234 portfolio backtest paths. The filter itself is clearly worth keeping (filter OFF yields a median CAGR of **-2.20%**, MDD -38.2%, PF 0.76) — but the old 60-day setting was not detecting regimes, it was **halting buys on ordinary dips**: KOSPI block episodes ran 7.6 per year with a median length of 3.5 days, 59% of them 5 days or shorter, and **82% were false alarms where the index actually rose during the block**.
    *   **Why 80 days**: Going shorter is worse — a 40-day MA loses to the 60-day baseline (41.9% path win rate). The 80-day beats the 60-day with a **74.4% path win rate, +2.43pp CAGR and +3.07pp MDD**, and is the only value that fails to lose in any of the 9 validation windows (100 and 120 are better on average but lose badly in specific windows). **150 days and above are ruled out** — they missed 3 of the 9 KOSPI declines of -15% or worse entirely, disqualifying them as a defensive layer (the 80-day missed 1).
    *   **What the band (hysteresis) does**: It matters more than the period. Requiring a 1% break to block and a 1% recovery to resume ignores three-day dips; even on the 60-day MA it lifts the path win rate to 76.9% (+2.78pp CAGR). Over-confirming a short period backfires, though (60-day ±2% shows no improvement). Set it to 0 to restore the previous simple-crossover rule.
    *   **Cost of the lag**: The 80-day blocks after an average 43% of a decline has played out (12 days) versus 40% (10 days) for the 60-day. Since this gate only blocks **new entries** — exits are handled by the ATR stop and the Chandelier trailing stop — that lag costs structurally less than the whipsaw it removes.
    *   **Limitation**: The portfolio validation universe is KOSPI-heavy (only 143 KOSDAQ signals), so KOSDAQ rests on index-level evidence alone. There too the 60-day had almost no discriminating power (forward 20-day index return of 0.56% when allowed vs 0.35% when blocked), and 100–120 days was superior.
*   **Relative Strength (RS) Filter — Retired (OFF by default, setting hidden)**: The binary rule "trails the index ⇒ no new buy" collides head-on with entering a trend at its start. Across 10,307 actual buy signals from the live universe (37 stocks × 9 years, 2016–2026), the RS gate rejected 28.6% of signals while making every metric worse: big winners (+30% over 120 days) 26.6%→25.4%, losers (-10% or worse) 24.1%→25.0%, MDD -14.33%→-14.71%. The cause is that stocks turning up *after* the index has already run are permanently stamped as laggards — the rejection rate peaks at 39.9% when the index's 6-month momentum exceeds +20%, and 48.6% of those 980 rejected signals went on to gain 30% or more. Performance across RS buckets is also non-monotonic (the RS ≤ -30pp bucket has the highest big-winner rate at 43.9%), so it does not work as a threshold gate. Its useful content is entirely captured by the stock's **absolute** momentum, already present as the "price momentum" scoring factor; adding RS on top of an absolute-momentum gate degrades every metric (big winners 28.7%→26.8%). `USE_RS_FILTER` now defaults to `False`; the gate logic and config key remain but are hidden from the settings menu and help screen, and can only be re-enabled by editing `dynamic_config.json` directly.
*   **Correlation Filtering**: Prevents new buys if the candidate stock shows high correlation (e.g., 0.7 or above) with currently held stocks to avoid concentration risk.
*   **Technical Filtering**: 
    *   **General Filter**: Suspends new buys if the trend breaks (MACD death cross, major MA breakdown).
    *   **Absolute Defense Filter**: Blocks new buys completely regardless of the score during "Super Panic" selling zones (ADX 45+ with strong sell pressure) to avoid fake rebounds.
*   **Daily Loss Limit (Defensive Mode)**: If the daily estimated asset drops beyond a set limit (e.g., -10%), **new buys and pyramiding add-ons are halted** — but the system is *not* shut down. Hitting the daily loss limit means several positions are already near their stops, so shutting down would also stop stop-loss and trailing-stop monitoring and leave those positions unattended ("if you don't cut losses, your account will eventually take serious damage"). Therefore only **exposure-increasing actions are blocked while exit monitoring keeps running**; the mode clears automatically when the date rolls over and the daily starting asset is re-measured. A full stop remains the user's call.
*   **Fail-Closed on Unknown Market Direction**: If index data cannot be retrieved and market direction is unknown, new buys are **suspended rather than allowed** ("if you don't know what's going on, do nothing"). Sell/stop-loss paths never consult index status, so they keep operating regardless of data outages.
*   **Stop-Loss Alerts for Positions Excluded from Auto-Sell**: Trading-restricted stocks (manual holds) and ETFs excluded from auto-trading are never stopped out by the system. When such a position breaches its stop level, a **Telegram alert** is sent instead of an automatic liquidation, so the user can decide. (Repeat alerts for the same stock are throttled to 24 hours and re-arm once the position recovers above its stop.)
*   **Max Holding Stocks (weight derived automatically)**: Limits the maximum number of stocks held in the portfolio. The per-stock weight (`SYSTEM_INVEST_PER_STOCK`) defaults to **0 = auto**, in which case it is computed as `1 ÷ max holdings`. Changing the slot count alone therefore **always keeps the nominal sum (weight × slots) at 100%**, eliminating the mistake of having to keep the two values paired (0.3 × 4 = 1.2 → the first-ranked names drain cash and later slots starve). A per-stock rule's weight also falls back to the global/auto value when set to 0, so a value saved earlier is never frozen out of sync with a later slot-count change.
    *   Entering a weight greater than 0 overrides auto and uses that value verbatim. You can keep the nominal sum below 100% to hold a cash buffer (4 slots × 0.2 = 80%), or deliberately exceed 100% — **intentional overcommit is allowed**, letting you open more slots than cash covers so top-ranked candidates spend it first. Exceeding 100% only prints a warning and is never blocked, since it can be a reasonable way to keep less cash idle when signals are scarce.
    *   The lower bound on slot count is set by **the probability of holding a winner** (at a 26% win rate, 1-0.74ⁿ: 70% for 4, 59% for 3, 45% for 2); the upper bound by **winner dilution and universe size** (stay within 15–20% of your watchlist). Suggested by seed: ≤2M KRW → 3 / 3M–10M → 4 / 15M–50M → 5 / 50M+ → 6. Sizing scales strictly with equity, so a larger seed changes only the absolute amounts — deployment rate and portfolio heat stay identical.
*   **Kill Switch**: Transitions to standby mode to protect the account if continuous API or system errors occur (default 5 times).
*   **Order State Machine**: Strictly manages the order lifecycle to prevent duplicate buys or phantom positions.
*   **Risk-based Position Sizing**: Limits the maximum loss width (default 4%) per trade against the total account, automatically reducing buy weight for highly volatile stocks.
*   **No stop, no entry**: If the ATR stop is off (or ATR is unavailable) *and* the fixed stop-loss rate is 0 (disabled), **new buys are skipped and the reason is logged**. Such a position would have no exit rule at all — and since risk-based sizing takes the stop width as input, its **loss cap would silently vanish too**. Unreachable under the defaults (ATR stop on, fixed -7%); it only triggers when both are disabled globally or in a per-stock rule.
*   **Backtest matches live data**: Domestic backtests use **official KRX data (pykrx/FDR) regardless of mode** (falling back to yfinance, then the chart API). Toss mode already computes live indicators from official KRX data and KIS daily bars are on the same KRX regular-session basis, so the strategy you validate and the strategy you run sit on identical data — and **switching modes no longer changes backtest results**. The analysis path (`get_chart_data`) is capped at 250 bars (~1 year) and cannot serve long backtests, so the source is kept identical and only the lookback window is extended.
    *   **No silent truncation**: if the run falls through to the chart API and the recovered history is shorter than requested, a **yellow warning** is printed. Previously a 5-year request could silently run on a single year because of the 250-bar cap.
*   **Portfolio Heat Cap (Total Open-Risk Limit)**: Caps the combined potential loss of all holdings — measured from current price down to each position's effective stop (including break-even/trailing uplifts) — at a set percentage of the account (default 10%, `SYSTEM_MAX_PORTFOLIO_RISK`). While the per-trade risk limit controls each position individually, this cap controls the 'simultaneous stop-out' scenario in aggregate: new buys are shrunk or held to fit the remaining risk budget, and pyramiding add-ons are also held when over budget. As existing positions' stops rise to break-even/trailing levels, the budget naturally recovers and buying resumes.
*   **Volatility Targeting**: Normalizes each stock's annualized volatility toward a target (default 25%, `TARGET_VOLATILITY`) using ATR — shrinking size for high-volatility names and expanding for low-volatility ones. Crucially, **the expansion is clamped so it never exceeds the per-stock nominal cap (base weight)**, preventing over-concentration in a single name. (Sizing respects the risk limits even on the final slot.)
*   **Three caps combined by min()**: The buy amount is the **smallest** of three independently computed caps: ① the base weight (anti-concentration), ② the risk-based cap (loss-amount control), and ③ the volatility cap (volatility normalization). ② and ③ are not stacked multiplicatively because, with an ATR-based stop, both are inversely proportional to ATR — multiplying them shrinks size as `1/ATR²` (it squeezed high-volatility names down to 21% of the base weight, and raising the base weight left the final amount unchanged). Under min() the final amount is always ≤ ②, so the loss-amount cap still holds exactly.
    *   ⚠️ `SYSTEM_INVEST_PER_STOCK` and `SYSTEM_MAX_HOLDINGS` set **concentration**, not **deployment**. If the nominal sum (weight × slots) is 100%, then 4×0.25 and 3×0.33 deploy the same amount when fully invested; actual deployment is governed by `TARGET_VOLATILITY`.
*   **Dynamic Risk Scaling**: Following the trend-following principle of "capping risk and volatility relative to capital," the risk limits for *new* entries (per-trade risk and portfolio heat cap) are automatically reduced based on market regime and account state. Only entry *size* is adjusted; exit logic (trailing stops, etc.) is never affected.
    *   **Regime-linked reduction**: The trigger is not a confirmed bear market but the **PendDown regime (the early stage of a trend breakdown)** — default ×0.6, `PENDING_DOWN_RISK_SCALE`. In the 15-year backtest a confirmed bear had already fallen 5%+, so its forward 20-day returns were actually positive and cutting risk there only shaved CAGR; hence the default is off (×1.0).
    *   **Whipsaw-linked reduction**: The higher the share of the last 8 crossovers that reversed without reaching the 5% confirmation threshold (the whipsaw ratio) — i.e. the choppier the market — the more entry size is continuously reduced. (Default: ≤40% → ×1.0, ≥75% → ×0.6, linearly interpolated in between.) This markedly lowers MDD versus the four-regime signal alone (KOSPI -41.7%→-34.6%, KOSDAQ -47.8%→-36.9%).
    *   **Drawdown step-down (Turtle-style)**: Risk limits are reduced in steps based on the account's drawdown from its high-water mark (HWM). (Default: ≥5% → ×0.75, ≥10% → ×0.5.) Betting shrinks automatically during losing streaks to smooth the max-drawdown curve, and restores on recovery. (Regime and drawdown factors combine multiplicatively.)
    *   **Gap-risk buffer**: Since the soft stop (periodic-check stop-loss) can fill below the stop price on a gap-down, risk-based sizing multiplies the stop distance by a buffer (default ×1.2, `GAP_RISK_BUFFER`) for conservative sizing.
    *   **Pyramiding market gate**: In a bear market where new buys are blocked (index < SMA), even a validated profitable position is held from adding on (no exposure expansion).

### 5. Scoring Weights Optimization
*   **Overview**: Allows users to manually configure or optimize the weights for each factor (Trend, Momentum, Strength, Synergy) in the buy score.
*   **Settings**: `TREND` (4.0), `MOMENTUM` (2.5), `STRENGTH` (1.5), `SYNERGY` (2.0).
*   **Optimization**: Use the 'Weight Optimization' feature in the backtesting menu to find the best combination based on historical data. The take-profit/stop-loss sweep in the single backtest run includes a **"no take profit (trailing-stop driven)" baseline** — fixed take-profit combinations are shown for reference against the trend-following doctrine.

### 6. Adaptive Thresholds
*   **Overview**: Dynamically adjusts the buy criteria score (`BUY_SCORE`) by classifying the market regime in real-time.
*   **Classification (dual-EMA crossover + follow-through)**: Uses Ed Seykota's rule. A crossover of the fast EMA (9-day, β=0.25) over the slow EMA (41-day, β=0.05) marks a change of direction, but the trend is only **confirmed once the index has advanced at least 5% since the crossover**. If it reverses before that, the segment is counted as a whipsaw (failed trend).
*   **Four regimes**:
    *   **Bull** — fast > slow, +5% achieved since crossover: lowers buy criteria (e.g., -0.5 points).
    *   **PendUp** — upward crossover but below the 5% threshold: keeps the default score (no easing before confirmation).
    *   **PendDown** — downward crossover but above -5%, i.e. **the early stage of a trend breakdown**: raises buy criteria (e.g., +0.5 points).
    *   **Bear** — -5% reached since crossover: raises buy criteria.
*   **Validation (15-year KOSPI/KOSDAQ backtest)**: The former method (index vs. EMA5 + ADX) flipped 71–73 times per year — effectively noise — and its "bear" label covered 42% of all days while showing no discriminative power over forward returns. The new method stabilizes at 12–13 flips per year, and forward 20-day index returns separate cleanly: Bull +2.9%/+0.9% vs. PendDown -0.5%/-0.6%.
*   **Configuration**: Adjust the score offsets (`BULL_SCORE_ADJ`, `PENDING_DOWN_SCORE_ADJ`, etc.) and classification parameters (`REGIME_EMA_FAST`, `REGIME_EMA_SLOW`, `REGIME_CONFIRM_PCT`) via `MARKET_REGIME_PARAMS` in `config.py` or the settings menu.

## 4. Architecture & Stability

This system applies a robust backend architecture to solve concurrency issues and API communication bottlenecks that can occur in multi-threaded auto-trading environments.

*   **DB Worker Queue Proxy & SQLite Lock Prevention**: 
    *   **Problem**: Simultaneous write attempts to a local SQLite database by multiple threads cause `database is locked` errors, leading to critical order omissions.
    *   **Solution**: Routes all DB writes through a single worker queue proxy (`db_queue.py`), ensuring data integrity and completely preventing DB locks.
*   **Global Thread Pool**: 
    *   Prevents memory leaks and context switching overhead from indiscriminate thread creation by centrally managing a system-wide thread pool in `executors.py`.
*   **Dynamic Configuration Validation & Thread-Safe Architecture**: 
    *   Applies a `threading.RLock` mechanism to prevent crashes when trading strategy settings are changed in real-time. Validates parameters using `Pydantic`.
*   **Order State Machine & Kill Switch**: 
    *   Strictly tracks the order lifecycle from reception to execution/cancellation. A Kill Switch pauses the system if continuous network/API errors occur.
*   **Unified Real-time Price**: 
    *   To ensure the analysis screens and the system trader compute indicators from the **same intraday price**, the logic that overlays the latest real-time price onto the unconfirmed daily candle (close/high/low) is unified into a single entry point (`indicators.apply_realtime_price`), structurally eliminating score mismatches between the menu analysis and auto-trading.
    *   Domestic price/trade-strength is fetched in **a single call covering both KRX and NXT (alternative exchange)**; in real-trading mode, NXT session quotes are reflected in real time, sharing the same cache key as the system trader to cut redundant calls.
    *   **KRX close after all sessions end (`USE_KRX_CLOSE_AFTER_HOURS`, ON by default)**: Once every session is over (NXT after-market closes at 20:00) — overnight, weekends, holidays — stock analysis **pins the current price and all indicators to the KRX regular-session close**. In that window the live price is frozen at the last NXT print, and letting it overwrite the confirmed KRX daily bar drags EMA, RSI, CCI, ATR, and the 52-week position along with it. Since every historical bar is KRX regular-session based (pykrx/FDR), matching that basis is the consistent choice.
        *   Measured (SK Telecom, 2026-07-24): KRX close 100,000 vs after-market 20:00 print 99,700 → **EMA5 94,805→94,705, RSI 61.8→61.54, CCI 231.6→230.10, 52-week position 55.2%→54.8%**
        *   During trading hours (pre 08:00–09:00, regular session, after 15:30–20:00) the live price is applied as before — you need the live market's price to react to it.
        *   **Order pricing always uses the live price regardless of this setting**, since a limit order priced at the KRX close would sit outside the NXT book and never fill.
        *   Turn it off under `[0] → 5-3. Data & Communication` to restore the previous behavior of reflecting the last NXT print.
    *   **NXT closing price retained after hours**: With the setting above turned off, real-trading mode uses the live NXT quote if KIS provides one; otherwise the **last NXT close is remembered and shown until the next trading day's open** (persisted to disk, survives restart). (Simulation always shows the KRX close, since the KIS API does not support NXT there.)
*   **Real-time WebSocket Quotes & Execution Notices**:
    *   Korea Investment & Securities (real/simulation) **WebSocket push** is used to receive the current price/trade-strength of held and candidate stocks without REST polling (`realtime.py`). The read paths (`get_current_price`/trade-strength) prefer the WS cache and **automatically fall back to REST** when a symbol is unsubscribed, disconnected, or the feature is off — so behavior is always guaranteed. Freeing the TPS budget for order/balance calls **improves system-trading throughput**.
    *   **System-trading symbols subscribed first**: KIS limits a single connection to 41 registrations (symbol×TR) and one concurrent connection per approval_key. System-trading symbols (holdings → buy candidates) are **always subscribed**, and remaining slots rotate the other watchlist symbols. Whether domestic ETFs are included in system trading (`SYSTEM_INCLUDE_ETF`) is also honored in the subscription priority.
    *   **Real-time execution notices (H0STCNI0/H0STCNI9)**: Order fills are detected instantly over WebSocket (AES256-CBC decryption), immediately waking the proven conclusion-confirmation logic (`ConclusionMonitor`). The simulation account's fill-estimation delay (up to minutes when idle) is **cut to near-instant**. On missing notices, decryption failure, or unset HTS ID, it **fully falls back to the existing periodic polling**. (Requires the **HTS login ID** as the subscription key — see environment variables below.)
    *   **Runtime toggle**: The on/off setting (`USE_WEBSOCKET`) can be changed under **Main Menu `[0] System Settings > 5-1`** and applies **without restarting** (off → REST fallback, on → auto-reconnect). WebSocket connection/subscription/execution-notice status is logged at **INFO level to the file log** for monitoring. (Toss Securities has no official WS support, so REST polling is retained.)
*   **Signal Ledger — observability for live-only entry gates**:
    *   Volume-strength, ask/bid-ratio and same-day re-entry blocking rely on **real-time order-book data**, so a daily-bar backtest cannot reproduce them. What those gates actually rejected can only be counted from live operating records.
    *   Previously that record existed only as log text and was deleted after 30 days, pinning the audit window at 18 trading days and leaving "is the gate a net gain or loss?" unanswerable. Parsing was also fragile — `[askbid:3.92]` (informational) and `askbid:3.92<1.0` (blocked) differ by one character.
    *   Decision points now write the outcome **directly to the DB (`signal_ledger`)**. Each **(date, code) pair is one row** accumulating per-cycle pass counts and per-reason block counts, so at most one row per watchlist symbol per day (~1.5 MB/year; retention `SIGNAL_LEDGER_RETENTION_DAYS`, default 3 years).
    *   This separates **"never passed all day" (fully blocked)** from **"blocked in some cycles only" (partially blocked)**. Counting the latter as blocked would inflate opportunity cost, since those symbols passed later in the day and were actually bought.
    *   **Account separation**: real and simulated accounts share one DB file, so ledger rows are split by `is_sim`. Re-entry and correlation blocks depend on that account's holdings, so mixing them makes the block rate meaningless (paper mode uses a separate DB file entirely).
    *   Analysis: `python3 tools/audit_signal_ledger.py [--db db/paper_trading.db] [--forward 20] [--account real|sim|all]`
    *   Auto-trading logs (`autotrade_*.log`) are audit evidence too, so they are retained separately and longer than general logs (`AUTOTRADE_LOG_RETENTION_DAYS`, default 120 days vs. `LOG_RETENTION_DAYS` 30 days).
*   **Memory Protection (Raspberry Pi OOM guard)**: 
    *   For long-running operation on constrained devices (e.g., Raspberry Pi 1GB), the quote micro-cache and chart cache enforce a **maximum item count** and evict the oldest entries when exceeded, so memory does not grow unbounded even during full-market scans.
*   **Interrupt-safe Exceptions**: 
    *   Bare `except:` clauses were normalized to `except Exception:` so that `KeyboardInterrupt`/`SystemExit` propagate correctly (preserving Ctrl+C responsiveness and clean shutdown).
*   **Process-death detection (dead-man switch) — it alerts, it never revives**:
    *   All monitoring used to live **inside the process** (the heartbeat only checked that the auto-trading *thread* was alive). So when the process itself disappeared — a Raspberry Pi OOM kill, an SD-card fault, a power blip — whatever was supposed to raise the alarm died with it: **Telegram stays silent while stop-loss and trailing supervision are gone.** Holding a position, you are unprotected until a human happens to look at the screen.
    *   The structure is inverted. The living process stamps `logs/heartbeat.json` every minute and writes down **when it promises to stamp again (a deadline)**. An external watchdog run by cron (`tools/hts_watchdog.py`) only checks whether that promise has expired — it needs no market calendar, no settings, no account, so it imports nothing heavy (the Pi's memory matters).
    *   **It never restarts anything.** Relaunching without knowing why the process died either repeats the death or, worse, brings up a half-alive process that places orders. The watchdog's one job is to tell a human; whether to revive is the human's call.
    *   Quitting from the menu or receiving `SIGTERM` leaves an "I am going down on purpose" marker, so **clean shutdowns raise no alert**. Conversely `SIGKILL` (OOM), a power cut, or an unhandled exception leaves the last stamp in place, the deadline passes, and the alert goes out. It never re-sends for the same death, and it sends one recovery notice when stamps resume.
    *   Installation is a single cron line (usage is documented at the top of `tools/hts_watchdog.py`). For a manual check: `tools/hts_watchdog.py --status`.
*   **Per-mode config profiles**:
    *   A single `json/dynamic_config.json` used to be shared by every mode. Turning the market filter off in paper mode (mode 4) to force trades **carried straight over into live trading (mode 2)**, and the only defense was a warning at startup — a design that bet on a human reading it.
    *   Live now reads and writes the baseline file only. Non-live modes overlay their own profile (`dynamic_config.sim/toss/paper.json`), and that file records **only the values that differ from the baseline**. As a result (1) changing live settings still propagates to the other modes, (2) changes made in another mode never leak into live, and (3) opening the file shows exactly how that mode differs from live.
    *   Promotion is manual: to use a value you liked in paper mode, boot into live and change it there. Removing the path by which a safety switch disabled "just for testing" quietly follows you into production is the entire point.
*   **API Call Efficiency & Speed (TPS Optimization)**:
    *   KIS/Toss OpenAPIs enforce a per-second transaction (TPS) limit, and every quote/order call passes serially through a single global TPS gate. Analysis time is therefore "total calls ÷ TPS", so the following improvements target both factors.
    *   **Adaptive Dynamic TPS (AIMD)**: Instead of a fixed margin (effective 18 TPS), the effective TPS starts from a margin and is additively raised as successes accumulate, then multiplicatively backed off the moment `EGW00201` (rate exceeded) occurs — **self-converging to the optimal TPS** for current server/network conditions.
    *   **NXT Time-window Gating**: During the regular session (09:00–15:30) KRX is the representative price, so the auxiliary NXT (alternative exchange) quote call is skipped and only fetched during NXT-only sessions (pre/after market), **roughly halving per-stock calls during regular hours**.
    *   **Persistent Daily-chart Cache**: Daily candles change once per day, so they are persisted to disk (SQLite) per trading day. After a restart they are restored instantly **without re-fetching over the network for the same trading day** (eliminating startup bursts); stale-date entries are auto-pruned.
    *   **Skip Unneeded Order-book Calls**: The order book (ask/bid ratio) is fetched only for stocks whose buy supply/demand gate is active, removing order-book calls for stocks with the gate disabled.
    *   **TPS-aligned Worker Pools**: Per-stock parallel analysis worker counts are aligned to TPS, preventing excess threads that merely wait at the gate (wasting memory).

## 5. Project Structure

```text
my-stock-hts/
├── run.sh                # [Mac/Linux] Execution script
├── run.bat               # [Windows] Execution script
├── main.py               # Main execution file (Menu & Routing)
├── config.py             # Settings, Env vars, Data load
├── api/                  # Quote & order API layer package (split from the former 7,596-line api.py)
│   ├── __init__.py       #   ├ Name re-export + patch propagation (callers still use api.func())
│   ├── instruments.py    #   ├ NXT tradability & domestic ETF/ETN classification
│   ├── market_calendar.py#   ├ Holidays (KR/US/exchange MIC) and overseas clocks
│   ├── sessions.py       #   ├ Session detection (regular/pre/after/day market) & screen labels
│   ├── yf_quotes.py      #   ├ yfinance/TradingView quotes + short-lived micro cache
│   ├── chart_cache.py    #   ├ Chart memory/disk cache, watchlist prefetch
│   ├── http.py           #   ├ TPS gate, retries, connection pool (ThrottledSession)
│   ├── auth.py           #   ├ Token issue/refresh and the shared call entry point (call_api)
│   ├── charts.py         #   ├ Daily/weekly/intraday chart fetch
│   ├── indices.py        #   ├ Indices & KOSPI200 futures
│   ├── quotes/           #   ├ Quote lookups
│   │   ├── nxt.py        #   │   ├ NXT quotes & multi-quote batching
│   │   └── price.py      #   │   └ Current price, order book, flows, overseas detail
│   ├── toss.py           #   ├ Toss Securities layer + domestic daily-bar fallback
│   ├── account.py        #   ├ Balances, fills, open orders
│   └── orders.py         #   └ Order placement/amend/cancel, deposits
├── brokers/              # Raw broker clients - they speak the broker's own wire format
│   ├── __init__.py       #   ├ Layer contract (why these are not flattened into the api namespace)
│   ├── toss_api.py       #   ├ Toss Securities Open API client (quotes/assets/orders in Toss mode)
│   └── realtime.py       #   └ KIS WebSocket real-time quote & execution-notice feed (REST fallback)
├── core/                 # Lowest-level shared layer - only code that knows nothing about the domain
│   ├── __init__.py       #   ├ Layer contract (never imports an upper layer at import time)
│   ├── constants.py      #   ├ Constant definitions (TR ID, field mapping, etc.)
│   ├── indicators.py     #   ├ Technical indicators calculation (RSI, ADX, MACD, etc.)
│   ├── utils.py          #   ├ Common utilities (dates, formatting, etc.)
│   ├── jsonio.py         #   ├ Shared JSON file load/save helper
│   ├── caching.py        #   ├ Shared in-memory TTL cache (size cap & auto-eviction)
│   ├── executors.py      #   ├ Global thread pools (AI / IO / Telegram sending)
│   ├── trading_cost.py   #   ├ Single source for trading-cost math (fees & taxes)
│   ├── session.py        #   ├ Session & token management (config resolved lazily via `_config()`)
│   └── context.py        #   └ Global thread states & Lock management
├── requirements.txt      # Single source of runtime dependencies (run.sh installs from this file)
├── requirements-dev.txt  # Development/test-only dependencies (pytest stack)
├── pytest.ini            # Pytest test configuration file
├── .env.example          # Env var setup example file
├── LICENSE.md            # License file
├── db/                   # [Auto-generated] SQLite DB file storage
├── json/                 # [Auto-generated] Dynamic settings & state/cache file storage
│   ├── stock.json              # Interest/monitoring stock list
│   ├── restricted_stocks.json  # Trading restricted stock list
│   ├── daily_asset_state.json  # Initial starting asset record for the day (for daily loss limit)
│   ├── dynamic_config.json     # Live baseline settings (changed during program execution)
│   ├── dynamic_config.sim.json    # [Mode profile] Values that differ only in simulation mode
│   ├── dynamic_config.toss.json   # [Mode profile] Values that differ only in Toss mode
│   ├── dynamic_config.paper.json  # [Mode profile] Values that differ only in paper-trading mode
│   ├── token_cache.json        # [Auto-generated] API access token cache
│   └── dart_corp_map.json      # [Auto-generated] DART stock code ↔ unique number (corp_code) mapping cache
├── logs/                 # [Auto-generated] Log file storage
│   ├── mystock.log             # Program log
│   ├── startup.log             # Boot record (the only clue when launched via cron @reboot)
│   └── heartbeat.json          # Process liveness stamp — read by the external watchdog
├── chart/                # [Auto-generated] Chart image storage
├── data/                 # [Auto-generated] Excel/CSV export storage
├── tools/                # Various diagnostics & utility tools
│   ├── stock-hts               # [Linux server] tmux session auto-configuration script
│   ├── hts_watchdog.py         # Process-death watchdog (cron) — alerts only, never restarts
│   ├── update_holidays.sh      # Periodic holidays-package refresh (removed from the boot path)
│   ├── get_telegram_chat_id.py # Telegram Chat ID confirmation tool
│   ├── clear_trade_history.py  # Trading history & DB initialization tool
│   ├── check_execution.py      # Execution history confirmation tool
│   ├── check_unfilled_orders.py# Unfilled orders confirmation tool
│   ├── check_deposit_apis.py   # Deposit-related API confirmation tool
│   ├── check_simulation_balance.py  # Mock investment balance confirmation tool
│   ├── benchmark_api_tps.py    # API TPS (calls per second) benchmark tool
│   ├── simulate_thread_workers.py   # Thread worker performance simulation
│   ├── verify_autotrader_cancel.py  # Auto-trading cancellation logic verification tool
│   ├── search_indices_yfinance.py   # yfinance-based overseas index search tool
│   ├── gemini_tool.py          # Gemini AI feature direct test tool
│   ├── get_google_genai.py     # Google GenAI SDK connection confirmation tool
│   ├── check_cb.py             # DART disclosure-based CB remaining balance check tool
│   ├── fetch_intraday_tv.py    # Intraday bar (tvDatafeed) fetch/cache tool
│   ├── journal_sync_e2e.py     # End-to-end verification for trading journal sync
│   ├── audit_common.py         # Shared audit contract (exit-sample definition) used by the tools
│   └── audit_*.py              # Strategy dial audit scripts (backtest-based, 80 files)
├── tests/                # Pytest unit/integration test codes (3,000+)
└── modules/              # Feature-specific module folders
    ├── db_manager.py     # DB connection & query management
    ├── db_queue.py       # Single worker queue proxy for SQLite concurrency control
    ├── telegram_bot.py   # Telegram bot inbound command handling
    ├── telegram_notify.py# Telegram outbound layer (message/photo sending)
    ├── dart_api.py       # OpenDART (disclosure) API integration (dividends/earnings/disclosures)
    ├── scheduler.py      # Dedicated worker for background scheduling & timers
    ├── heartbeat.py      # Process liveness stamp & verdict (alert on death; never auto-restart)
    ├── market_halt.py    # Circuit breaker (CB) / VI market-halt detection & Telegram alerts
    ├── prompts.py        # External management of prompt templates for AI assistant
    ├── settings.py       # [0] System Settings management
    ├── market.py         # [1] Market Indices inquiry
    ├── analysis.py       # [2] Stock price & technical analysis
    ├── chart.py          # [3] Chart visualization & analysis
    ├── backtest.py       # [4] Strategy Backtesting
    ├── portfolio_backtest.py # Multi-stock portfolio backtest (slot contention, cash limits, heat cap)
    ├── intraday_bars.py  # Intraday bar collection/cache (tvDatafeed) for fill-timing checks
    ├── krx_daily.py      # KRX regular-session daily bars (pykrx/FDR)
    ├── auto_trade/       # [5] System Trading (Auto Trading) package
    │   ├── common.py     #   ├ Shared helpers (restricted stocks/daily asset/market hours/OrderStatus)
    │   ├── engine.py     #   ├ Trading engine (DefaultStrategy·OrderManager·RiskManager)
    │   ├── conclusion.py #   ├ Fill monitoring/confirmation (ConclusionMonitor)
    │   ├── trader.py     #   ├ AutoTrader main loop (analyze→buy/sell→report)
    │   └── menu.py       #   └ Trading rules/restricted stocks menu UI
    ├── paper_broker.py   # Paper-trading virtual broker (intercepts balance/orders at the api layer)
    ├── instance_lock.py  # Single-instance guard for auto trading (per-account)
    ├── theme_analysis.py # [6] Discovery & Financials + AI (Gemini) analysis/disclosure summary
    ├── manage/           # [7] Watchlist Management + [6-5~8] fundamentals package
    │   ├── watchlist.py  #   ├ Watchlist add/delete/view & menu UI
    │   ├── discover.py   #   ├ [7-4] Candidate discovery (rule-based screening, multi-select add)
    │   ├── events.py     #   ├ [6-5] Dividend/Earnings Calendar (DART + yfinance)
    │   ├── econ_events.py #   ├ Major economic event schedule (FRED + Fed calendar)
    │   ├── disclosure.py #   ├ [6-6] Disclosure monitoring/earnings tracking + Telegram alerts (DART)
    │   ├── insider.py    #   ├ [6-7] Supply-demand & overhang signals (treasury stock, mezzanine)
    │   └── financials.py #   └ [6-8] Financial snapshot (DART key accounts, YoY change)
    ├── trading.py        # [8] Stock Order Management (Buy/Sell/Modify/Cancel)
    ├── reserved_order_monitor.py # Background reserved order (Stop loss, Trailing, etc.) monitoring thread
    ├── account.py        # [9] Asset & Balance Management
    ├── paper_report.py   # [9-6] Paper account management & reports
    ├── holdings_backfill.py # Restores holdings trade history into DB from broker fills
    └── journal_sync.py   # Trading journal web server sync (Outbox pattern)
```

## 6. Prerequisites

This program operates based on the Open APIs of **Korea Investment & Securities (KIS)** and **Toss Securities**.
You need an account and API access key from the brokerages to run the program normally. (You can register both or choose just one.)

### Korea Investment & Securities (KIS)
1.  **Open Account**: Via smartphone app ('Korea Investment').
2.  **Apply for KIS Developers**: Apply on the KIS Developers website.
3.  **Mock Investment (Recommended)**: Apply for mock trading via the website/HTS.
4.  **Issue API Key**: My Page > My Services > Issue Key (Real/Mock separately).
5.  **(Optional) HTS login ID**: To use real-time **execution-notice WebSocket**, your KIS HTS login ID is required as the subscription key. (If unset, fill detection works via the existing REST polling.)

### Toss Securities
1.  **Open Account**: Via the Toss app.
2.  **Toss Developer Center**: Apply on the Toss Securities Open API website.
3.  **Issue API Key**: Issue App Key & Secret from the Developer Center.

### Common
1.  **Environment Variables**: Register the issued Keys and Account Numbers as System Environment Variables.
2.  **IP Allowlist (Whitelist)**:
    - **Toss Securities (required)**: The Toss Open API **requires registering allowed IPs**. Running from an unregistered network (mobile tethering, VPN, a line whose IP changed, etc.) causes token issuance to be rejected with `IP address not allowed`. Register the **public IP of the server/PC that runs the program** in the developer console (App Settings → Allowed IP).
    - **KIS (optional)**: KIS does not require IP registration by default. It only restricts access if you have set a "Customer IP" restriction on the API key. (Connection-refused / timeout errors are usually **KIS server maintenance/outage or a network issue** — check the server status and your network first.)
    - For always-on operation (e.g. a Raspberry Pi), a **static IP** line is recommended. If the public IP changes, you must re-register it.
    - If token issuance fails, the program **prints your current public IP and cause-specific guidance during the pre-flight check** — follow it.

## 7. Installation & Execution

### 1. Download Source Code
```bash
git clone https://github.com/your-username/my-stock-hts.git
cd my-stock-hts
```

### 2. Create and Activate Virtual Environment
**[Recommended]** It is highly recommended to use a virtual environment.
```bash
python3 -m venv .venv
source .venv/bin/activate  # macOS/Linux
# .venv\Scripts\activate   # Windows
```

### 3. Install Libraries
```bash
pip install -r requirements.txt

# For development and testing as well (includes the pytest stack)
pip install -r requirements-dev.txt
```
*Note: running `run.sh` handles this automatically. `requirements.txt` is the **single source of truth** for dependencies — `run.sh` reads that file at startup to scan for and install anything missing, so the list is never kept in two places. To add a dependency, edit `requirements.txt` only; add a line to `run.sh`'s `_import_name()` table only when the PyPI name differs from the import name.*

*The `holidays` package is **not** auto-upgraded on every startup. If the holiday-calendar library silently changes on each boot, market-hours decisions change without anyone knowing. Refreshing it (for ad-hoc public holidays) is split out into a periodic job instead — `tools/update_holidays.sh` (a weekly cron is recommended).*

### 4. Configuration
Register sensitive information like API Keys as **environment variables**:
*   `SIM_APP_KEY`, `SIM_APP_SECRET`, `SIM_ACC_NUM`: KIS Mock Investment
*   `REAL_APP_KEY`, `REAL_APP_SECRET`, `REAL_ACC_NUM`: KIS Real Investment
*   `AUTO_APP_KEY`, `AUTO_APP_SECRET`, `AUTO_ACC_NUM`: KIS Auto Trading Only (Optional)
*   `VIRT_APP_KEY`, `VIRT_APP_SECRET`: API key dedicated to **paper trading (mode 4)** (Optional). KIS enforces TPS, concurrent WebSocket, and token-issuance limits per app key, so a separate key keeps paper trading from eating into the live instance's order path.
*   `VIRT_ACC_NUM`: **Display-only** account number for the paper-trading instance (Optional). It appears in alert footers as `[RasPi3B | PAPER 43486025-01]`, identifying which account the instance runs on behalf of. **It is never used for trading or lookups** — the paper session's internal account number is always the literal `PAPER` as a fail-safe. If unset, the footer shows just `PAPER` as before.
*   `REAL_HTS_ID`, `SIM_HTS_ID`: Subscription key (KIS HTS login ID) for real-time **execution-notice WebSocket** (Optional). Set per real/mock; if identical, a single `KIS_HTS_ID` (or `HTS_ID`) suffices. **If unset, fill detection falls back to REST polling.**
*   `TOSS_APP_KEY`, `TOSS_APP_SECRET`, `TOSS_ACC_NUM`: Toss Securities
*   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: Telegram Bot (Optional)
*   `GEMINI_API_KEY`: Google Gemini API Key for AI features (Optional)
*   `GEMINI_MODEL`: Gemini model name to use (Optional, default: `gemini-flash-latest`)
*   `GEMINI_FALLBACK_MODEL`: Lighter fallback model automatically retried when the free-tier quota is exceeded (429) (Optional, default: `gemini-flash-lite-latest`)
*   `DART_API_KEY`: OpenDART API Key for disclosures (Optional)
*   `FRED_API_KEY`: FRED API Key for US economic release dates — CPI, employment report, PCE, etc. (Optional, [how to get one](#fred-api-key-free))
*   **KRX Gold Spot (KRW/g)**: `KRX 금현물` — listed in the Domestic Indices group right below KOSDAQ150 — is the spot price of 'Gold 99.99_1Kg' on the KRX gold market (the `금` row under Commodities is COMEX gold futures in USD/oz, so the two side by side reveal the domestic premium). The daily series comes from **official KRX** first (requires `KRX_ID`/`KRX_PW`), which supplies real OHLC and volume — so **OBV renders normally and the 52-week high is intraday-based**. Without those credentials it falls back to **Naver's commodity feed** (no API key required); Naver's daily series provides **closes only**, so in that case there is no volume history: **OBV shows `-`** and the 52-week high becomes close-based. KRX only serves settled bars after the close, so the **intraday live price always comes from Naver** and is overlaid onto the last bar (60s cache for the live price, 6h for the series). The open marker (`∙`) and holiday handling follow the KRX regular session (09:00–15:30 KST). In **[9]-5 Position Analysis**, entering the ticker `KRXGOLD` at the direct-input prompt registers it like a stock (quantity in grams) and runs the same exit rules (ATR stop, trailing stop) against it.
*   `TV_USERNAME`, `TV_PASSWORD`: TradingView account (Optional). Lets tvDatafeed (indices, US Treasury yields) run in **logged-in mode**, with better quotas/stability than anonymous access. If unset, it stays anonymous (nologin) as before and a WARNING is written to the file log (INFO on successful login). The issued token is cached in `data/tv_token.json` for 7 days so restarts don't re-login (frequent logins trigger TradingView's captcha). If a captcha is returned, retry later or sign in once from a browser and restart.
*   `KRX_ID`, `KRX_PW`: **[data.krx.co.kr](https://data.krx.co.kr) member account** (KRX Information Data System) (Optional). These are web login credentials, not an API key. They let several feeds come from **official KRX values**. Without them every item below falls back to its previous source, so behaviour is unchanged.
    *   **Historical daily investor flows** (foreign / institutional net buying) - used by the backtest's "smart money" signal. The fallback, the KIS investor TR, has **no date-range parameter and returns only the last 30 trading days**, so in a multi-year backtest the smart-money signal is treated as off for everything outside that window. (Across the overlapping 30 days the two sources match 30/30 on both foreign and institutional figures - KRX is the origin and KIS relays it.)
    *   **KRX spot gold** - the fallback (Naver) returns **close only**, with open/high/low coming down as 0, so every bar had to be flattened to its close (distorting ATR/ADX; no volume history means no OBV). KRX gives real OHLC and volume (0 mismatches against Naver closes over the overlapping 60 days).
    *   **KOSPI 200 / KOSDAQ 150 daily bars** - the fallback (tvDatafeed) intermittently returns empty responses and reports index volume as 0. **KOSDAQ 150 in particular has no ticker at all on Yahoo or FDR**, making tvDatafeed a single point of failure. KRX settled bars now form the backbone of the indicators, with only the current day overlaid from the realtime source (KOSPI, KOSPI 200 and KOSDAQ closes match FDR 399/399).
    *   **V-KOSPI 200** - previously KIS-live-only (mode 2), so it was **absent from the index list in Toss (3) and simulated (1) modes**. KRX provides it, so it now appears in every mode (its value matches KIS sector code 0503 - identical index, change and EMA5/20/60). KRX serves **settled bars only** and this index has no alternative realtime source, so in modes 3 and 1 it shows the last settled value during KRX regular hours (09:00-15:30 KST); the index screen marks those rows with `=` instead of the open marker `∙`. KRX also gives it as a close-only series (the spot price carried in the volatility-futures response), so its 52-week high is close-based and high/low-derived indicators such as ADX are limited.
    *   **KOSPI 200 futures stay mode-2 only.** KRX serves settled bars only, and futures sessions cover most of the day (day 09:00-15:45, night 18:00-06:00), so the value would be **up to a day stale throughout trading hours** - measured during a night session, KIS live read 1,034.85 against KRX's settled 1,074.55, a 40-point (3.8%) gap with the change percentage computed off the wrong baseline. Toss does not carry futures and tvDatafeed symbol search is blocked, so rather than display an inaccurate figure the row is **left out of the list** in modes 3 and 1.
    *   **Listed-stock master** (names and market caps) - used to validate tickers in AI output. Official KRX is primary with FDR as fallback; KONEX, which KRX does not cover here, is filled in from FDR.
    KRX only serves **settled bars after the close**, so intraday current prices still come from the existing realtime sources. **Note**: this is a password, so use a dedicated account you don't reuse elsewhere. Restart after setting it.
*   The index screen fetches indices in parallel workers, but tvDatafeed and yfinance calls are each serialized behind a global lock (source protection). A **stall detector** (`INDEX_FETCH_STALL_SEC`, default 60s) keeps one stuck index from holding the whole screen — it measures time elapsed with *no* index completing, not total runtime; past that, remaining indices render as `수신 실패 (N초 내 미응답)` and the rest of the table is drawn (0 waits indefinitely). When one symbol exhausts its retries a **circuit breaker** opens so subsequent tvDatafeed calls try only once (120s; a single success closes it).
*   `JOURNAL_API_URL`, `JOURNAL_API_KEY`: Sync fills to a remote trading-journal web server (Optional, [details](#13--trading-journal-sync)). Both must be set for the integration to turn on.
*   `JOURNAL_SOURCE`: Server-side sync scope identifier (Optional, default: `my-stock-hts`). **Must differ per machine (installation).**
*   `JOURNAL_BOT_ID`: Prefix for the bot identifier (Optional, default: the `JOURNAL_SOURCE` value). The actual `botId` appends the runtime mode and account (`raspi:real:68029263`), because the mode comes from the `--mode` CLI flag and is invisible to environment variables.
*   `JOURNAL_BOT_LABEL`: Display name shown on the web dashboard (Optional, default: generated from the trading account and environment)
*   `JOURNAL_SYNC_SIMULATION`: Set to `1` to also send mock-trading fills (Optional, default: `0`)

**Example (`export` in a shell profile such as `~/.htsrc`):**
```sh
# Korea Investment & Securities (real/mock)
export REAL_APP_KEY="..."  ; export REAL_APP_SECRET="..."  ; export REAL_ACC_NUM="12345678-01"
export SIM_APP_KEY="..."   ; export SIM_APP_SECRET="..."   ; export SIM_ACC_NUM="50012345-01"

# (Optional) real-time execution-notice WebSocket key = KIS HTS login ID
export REAL_HTS_ID="myhtsid"   # real
export SIM_HTS_ID="myhtsid"    # mock (a single KIS_HTS_ID covers both if identical)

# (Optional) trading-journal web server sync
export JOURNAL_API_URL="https://memo.example.com"   # HTTPS recommended (so the API key isn't sent in the clear)
export JOURNAL_API_KEY="skm_..."                    # issued from the web dashboard settings
```
> After adding/changing env vars, reload the shell (`source ~/.htsrc`) and **restart the program** for them to take effect.

### 5. Execution
```bash
chmod +x run.sh
./run.sh

# Auto-trading mode right away
./run.sh --mode 1 --auto
```

## 8. Telegram Bot

We recommend integrating a Telegram Bot to receive trading history notifications and remotely control the system.

### 1. Create Bot (BotFather)
1. Search **@BotFather** on Telegram.
2. Type `/newbot` and follow the guide.
3. Copy the **API Token**.

### 2. Check Chat ID
1. Send a message to your newly created bot (e.g., "start").
2. Run `python tools/get_telegram_chat_id.py` and enter your token to get the **Chat ID**.

### 3. Environment Variables
Register `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `config.py` or as environment variables.

### 4. Main Commands
*   **System Control**: `/start`, `/stop`, `/restart`, `/status`, `/health` (operations monitoring), `/config` (strategy settings)
*   **Account & Assets**: `/balance`, `/holdings`, `/pending` (unfilled orders), `/reserves` (reserved orders), `/profit [period]` (realized P&L, d/w/m/n), `/history [period]` (trade history), `/report [period]` (performance report), `/stats [stock]` (per-stock performance)
*   **Market & Stock Analysis**: `/market [group]` (indices, k/u/s/r/g/c/b), `/signal <stock/index>` (technical diagnosis), `/analyze <stock/index>` (AI in-depth diagnosis), `/chart [period] <stock/index>` (chart, d/h/m), `/briefing` (on-demand AI market briefing), `/closing` (AI closing briefing), `/curate` (AI leading-stock curation), `/scan [market]` (TradingView scan, k/u), `/news <stock>` (AI latest news), `/calendar [days]` (economic events & dividend/earnings schedule, default 30 days), `/ask <question>` (free-form AI Q&A)
*   **Management & Misc**: `/stocks` (watchlist), `/rules [stock]` (per-stock trading rules), `/restrict` (restricted stocks), `/addrestrict <stock> [reason]`, `/delrestrict <stock>`, `/memo [a/d/stock]`, `/log` (recent logs), `/help`

> Commands may be added or changed over time. **Type `/help` in the bot for the latest full list.**

## 9. Disclosure Integration (OpenDART)

By integrating the Financial Supervisory Service's **DART OpenAPI**, you can use features like disclosure monitoring, dividend/earnings calendars, supply-demand & overhang signals, financial snapshots, and real-time Telegram alerts for major disclosures. (Under Menu `[6] Discovery & Financials`)
> The DART API Key is **optional**.

1.  **Issue API Key (Free)**: Apply on the OpenDART website.
2.  **Environment Variable**: Register `DART_API_KEY`.
3.  **Features** (Menu `[6]` Discovery & Financials):
    *   `[5] Investment Calendar`: Leads with **major economic events** (FOMC rate decisions & minutes; US CPI, employment report, PCE, PPI, GDP, retail sales; Korean & US quadruple-witching expiry) shown with D-day countdowns — see [FRED integration](#fred-api-key-free) — followed by the watchlist dividend/earnings schedule. Estimates the next ex-dividend date per dividend cycle, and when a cash/in-kind dividend decision disclosure exists, parses the document to replace the estimate with the **confirmed record date and dividend per share**. Also shows estimated Korean earnings announcement dates (based on last year's provisional-earnings filing pattern) and the next statutory report deadline.
    *   `[6] Disclosure Monitoring`: Classifies recent disclosures by importance with Gemini AI good/bad-news summaries. Auto-extracts details: provisional earnings (revenue/OP/NP with YoY), paid-in capital increases (dilution), CB/BW terms, **supply contracts (amount, % of revenue, counterparty)**, **treasury stock decisions (amount, period)**, **bonus issues (allotment ratio)**, and **capital reductions (ratio)**.
    *   `[7] Supply-Demand & Overhang Signals`: (1) **Treasury stock acquisition/disposal/trust decisions** (company-level supply signal), (2) **mezzanine (CB/BW/EB) overhang watch** — conversion price vs. current price, potential conversion volume vs. shares outstanding, recent conversion-exercise filings, (3) **bonus issue decisions**, (4) insider (elestock) and 5% holder (majorstock) net buy/sell summary (per-stock **last report date**, report count, net change) plus detail.
        *   Note: DART does not expose the reason for a holding change, so **non-trading events are removed by pattern**: (a) new/re-filed reports put the *entire* holding in the change column, which is corrected by **differencing successive holding quantities** (e.g. NPS re-filing `+1,281,813` is really `+12,343`); (b) when 5+ executives file on the same day in the same direction it is treated as a **bulk grant (ESOP/stock grant)** and excluded from both summary and detail (mixed directions are kept as genuine trading).
    *   `[8] Financial Snapshot`: Revenue/operating profit/net profit with YoY from the latest periodic report, **standalone quarterly operating profit** (cumulative-difference method), and DART-computed **ROE / debt ratio**.
    *   **Telegram Alerts**: Sends instant pushes for major disclosures (capital increase, administrative issues, etc.).
    *   **Calendar Telegram Alerts** (ON by default): Once a day at `AUTO_CALENDAR_ALERT_TIME` (default 08:20) the scheduler pushes a **single digest** of today's (D-DAY) and tomorrow's (D-1) items — major economic events plus watchlist ex-dividend/earnings dates — with duplicate suppression. Disable via `AUTO_CALENDAR_ALERT_USE`; request it anytime with `/calendar [days]` in Telegram.

### FRED API Key (Free)

The **major economic events** section at the top of `[5] Investment Calendar` merges three sources. Only the US release dates need a FRED API Key; the rest work without one.

| Events | Source | API Key |
| --- | --- | --- |
| US CPI · employment report · PCE · PPI · GDP · retail sales · JOLTS | [FRED](https://fred.stlouisfed.org) (St. Louis Fed) | **Required** |
| FOMC rate decisions · minutes · Beige Book | Federal Reserve official calendar (`federalreserve.gov`) | Not needed |
| Korean & US quadruple-witching expiry | Computed locally (KR: 2nd Thursday / US: 3rd Friday of Mar/Jun/Sep/Dec) | Not needed |

> The FRED API Key is **optional**. Without it only the US release dates are omitted — FOMC and Korean events still show, and every other feature works normally.

**How to get one:**
1. Go to the [FRED API Keys page](https://fredaccount.stlouisfed.org/apikeys) → create a free account if needed (email verification).
2. Click **Request API Key**, briefly describe your use, and submit (issued instantly, free).
3. Copy the **32-character key**.
4. Register it as `FRED_API_KEY` and restart the program:
   ```sh
   # add to a shell profile such as ~/.htsrc
   export FRED_API_KEY="your_32_char_key"
   ```

**Behavior:**
*   FRED publishes scheduled release dates ahead of time, so the dates shown are **official confirmed release dates**, not estimates. (US local time — typically the following early morning in KST.)
*   When an expiry date falls on a market holiday it is **rolled back to the previous business day** (this applies in 2026 and 2027, when the US third Friday of June coincides with Juneteenth).
*   Results are **cached daily** in `json/econ_calendar_cache.json`, so re-running on the same day makes no external calls. On network failure it falls back to the previous cache. A cache in which some source failed is re-fetched even on the same day, so a transient timeout can't freeze an incomplete calendar for the rest of the day.
*   Events with no machine-readable forward schedule (e.g. Bank of Korea rate decisions) can be entered manually in `json/econ_calendar_seed.json` (refresh once a year). The `_help` block inside that file documents the format.

> **Filing-date handling**: DART provides the date field inconsistently across API families. The disclosure-list family (`list`/`elestock`/`majorstock`) returns `rcept_dt`, but the **material-report "decision" family** (treasury stock, mezzanine, bonus issue, capital reduction) omits `rcept_dt` entirely — the filing date lives in the **first 8 digits of the 14-digit receipt number**. The system recovers the date from the receipt number in that case. (Without it the date column renders blank and date-based newest-first sorting is silently defeated.)

## 10. AI-Powered Assistant

*   **Overview**: Combines Google Gemini LLM's real-time web search capabilities with precise technical analysis to provide deep investment insights.
*   **Key Services**:
    1.  **AI In-depth Stock Diagnostics (`/analyze`)**: Writes future stock price prospect reports based on quant scores and news.
    2.  **Market-Leading Theme Analysis**: Analyzes top 5 themes and leading stocks.
    3.  **AI Pre-market Briefing**: Daily morning briefings summarizing global markets and hot issues.
    4.  **Interactive Q&A (`/ask`)**: Ask free-form questions about stocks/economy based on the latest news.
    5.  **AI Backtesting Diagnostics**: Proposes optimal parameters (entry hurdles, stop width, trailing multiplier, weights — within the trend-following framework) evaluating backtest results.
    6.  **AI Trading Autopsy**: Reviews each closed trade. Because trend following runs a low win rate in pursuit of payoff ratio, the verdict is on **process, not outcome** (was the entry rule-compliant, did the exit work as designed). A rule-compliant loss is labeled "rules worked as designed", and no parameter change is proposed from a single trade.
    7.  **AI Closing Briefing (`/closing`)**: Daily market review and analysis of held stocks.
    8.  **AI Curation (`/curate`)**: Discovers leading themes and recommends stocks based on real-time macro indicators.
    9.  **AI Chart Image Reading (Gemini Vision)**: The chart image generated by Chart Analysis (menu `[3]`, weekly/daily supported) is read directly by the Gemini vision model, providing an in-depth diagnosis of candle patterns, trendlines, and volume profiles — just as a human would read the chart.
*   **Shared guardrails across AI reports**:
    *   **Trend-following doctrine injected**: Every prompt that yields a trading or allocation judgment (stock/index/chart analysis, backtests, trade autopsy, closing briefing) states the system's doctrine so counter-trend advice such as fixed take profits or averaging down is never generated. Chart reading in particular **forbids sell calls based on RSI overbought or CCI overheating alone**.
    *   **Disproven proposals blocked**: Items already rejected by backtests (RS filter, dead-cat bottom fishing, unifying the market-regime gate) are passed into the prompt with their evidence (signals tested, metric deltas) so the same suggestions don't come back.
    *   **Confidence and falsification**: Forward-looking reports use conditional scenarios instead of flat predictions, and every conclusion carries a confidence level (high/medium/low) plus **"what would show this call is wrong"**.
    *   **Ticker verification**: Any `Name(6-digit code)` the AI writes is cross-checked against the KRX listing, flagging **non-existent codes, name mismatches, and sub-100B KRW market caps** inline. (Skipped silently when the listing can't be fetched, to avoid false alarms.)

**Google AI Studio Integration**: Get a free API Key from [Google AI Studio](https://aistudio.google.com/) and register it as `GEMINI_API_KEY`.

## 11. Reserved Order System

Supports **quant score and technical indicator-based reserved orders**, fully utilizing local computer resources.
Background monitoring runs every 3 seconds and is persistently saved in SQLite DB.

### Trigger Conditions (9 types)
1.  **STOP**: Sell when price drops below target.
2.  **BREAKOUT**: Chase buy when breaking resistance.
3.  **LIMIT**: Execute at a specific price.
4.  **TIME**: Execute unconditionally at a set time.
5.  **SCORE**: Triggered by quant score thresholds.
6.  **RSI**: Triggered when RSI crosses limits.
7.  **TRAILING_BUY**: Chase buy after a rebound from the lowest point.
8.  **TRAILING_SELL**: Sell to preserve profits after dropping from a peak.
9.  **EMA**: Execute when crossing moving averages.

### Smart Protection Logic
*   **Expiration Date**: Auto-cancels upon expiry (Today, This week, This month, Indefinite).
*   **Duplicate Prevention**: If a stock with a reserved sell is sold manually, the reserved sell is auto-cancelled.
*   **Network Optimization**: API quote queries are completely blocked during off-hours (20:00~08:00) and the single-price auction breaks (08:50~09:00, 15:20~15:30) to prevent traffic waste and mock trading rate limit exhaustion.

## 12. Known Issues

*   **Unfilled Order Query Error (KIS Mock)**: Due to a KIS API bug, unfilled order queries may return empty lists even if the order was successfully placed. The system handles this via local order state tracking and blind cancellations.
*   **NXT and SOR Unsupported (KIS Mock)**: NXT real-time quotes and SOR unified orders are not supported in the KIS mock trading environment; only KRX regular-session trading is available. As a result, trading attempts during the post-15:30 NXT session raise errors, and **the price on the analysis screen stays frozen at the regular-session close and does not update** (because NXT quotes cannot be retrieved). They only work in the real investment mode.

## 13. 📓 Trading Journal Sync

Streams your fills to a remote trading-journal web server ([stock-memo](https://github.com/batmi/stock-memo)).
The protocol follows [`UniversalTradingHistoryAPI.json`](UniversalTradingHistoryAPI.json) (OpenAPI 3.1), a contract both projects share.

### Setup

**① Toggle** — `Menu 0 → 5. Environment & System → 3. Data & Comms → Trading Journal Sync`
Defaults to OFF. Takes effect immediately and persists to `json/dynamic_config.json`. While off, fills are not even queued (anything already queued is kept and resumes when you turn it back on).

**② Credentials** — environment variables (add to `~/.htsrc`, then restart)

```sh
export JOURNAL_API_URL="https://memo.example.com"   # required (HTTPS recommended)
export JOURNAL_API_KEY="skm_..."                    # required (Web dashboard → Settings → HTS API key)
export JOURNAL_SOURCE="my-stock-hts"                # optional, must differ per installation
export JOURNAL_BOT_ID=""                            # optional (defaults to JOURNAL_SOURCE)
export JOURNAL_BOT_LABEL=""                         # optional (web display name, auto-generated)
export JOURNAL_SYNC_SIMULATION="0"                  # optional, 1 also sends simulated fills
```

Both parts must be in place. If the toggle is on but the URL or key is missing, the menu tells you exactly what is absent.

> **Running several instances**: `JOURNAL_SOURCE` must differ per machine — sharing one lets each instance advance the other's backfill watermark, leaving gaps that never get scanned. The bot identifier is generated automatically from `JOURNAL_BOT_ID` + runtime mode + account (`raspi:real:68029263`), so you normally leave it alone: the trading mode comes from the `--mode` CLI flag and is invisible to environment variables.

### How it works — the outbox pattern

The fill path never touches the network. `insert_trade()` writes to `journal_outbox` **in the same transaction** as the trade record, and a background worker ships batches every 30 seconds (exponential backoff on failure). If the machine loses connectivity or reboots, the queue survives in the DB and drains afterwards; the server deduplicates on `brokerExecutionId`, so resending is always safe.

The queue covers **downtime on the server side**, but fills that happened while sync itself was off never entered the queue. Hence a second layer:

| Failure | Covered by | Recovery |
|---|---|---|
| Web server down / network cut | Layer 1: queue | Drains automatically once reachable |
| Sync toggled off / env vars missing | Layer 2: backfill | Compares local `trades` against the server's last-sync point |

Backfill runs once 60 seconds after startup, then every 6 hours. (Without the `trades:read` scope on the API key, only backfill stops working.)

After the server **explicitly rejects** an entry 5 times it is dead-lettered and the count is surfaced when you enable sync. Transport failures are not counted — counting them would discard a healthy queue just because the server was down for a while.

### Restoring records deleted on the web — resync

The bot only remembers that it *sent* something; it never checks whether the record still exists. So deletions on the web are not undone automatically — otherwise a deliberately deleted record could never stay deleted.

To restore, use **Account Settings → 🔄 Resync** on the web and pick quarter / half-year / year (all rolling). The command rides the bot's next ping (≤10s), and the server filters duplicates, so **err on the side of a longer window.**

**Restoring the web server from a backup always requires a resync.** Records sent after the backup are marked delivered in the bot's queue, so backfill will not catch them.

> `pause`/`resume` in the API spec are **deliberately not implemented.** Resync means "resend data that is already mine", whereas `pause` would let the web server halt a trading bot — a compromised web app could then freeze the bot while it holds positions.

### What gets sent

| Order status | Sent | Notes |
|---|:---:|---|
| Filled | ✅ | `confidence=CONFIRMED` |
| Filled (estimated) | ✅ | `confidence=ESTIMATED` — inferred from balance reconciliation |
| Accepted / Cancelled | ❌ | Not a trade record |
| Simulated (`is_sim=1`) | ❌ | Enable with `JOURNAL_SYNC_SIMULATION=1` (server stores them separately as `isSimulated`) |

Beyond symbol, quantity and price, each entry carries realized P&L, strategy score, stop-loss rate, the trade rationale and the order source, so the server can compute win rate and P&L correctly. The rationale lives only on the `Accepted` row locally, so the original order is looked up and its reason attached to the memo — **always narrowed by date**, because broker order numbers are reused every business day.

The idempotency key is `{env}:{account}:{fillDate}:{orderNo}:{status}`. Using the order number alone would make a different day's fill look like a duplicate and get silently dropped.

### Trade classification — what counts as "system"

The HTS reports **every** fill in the account, including orders a human placed in the broker's own app (detected via balance reconciliation). Lumping them all together would merge manual trading into automated performance, so the order source decides.

| Order source | `isSystem` |
|---|:---:|
| `(AUTO)` — placed by AutoTrader on a strategy signal | `true` |
| `(Reserved)` / `(Manual)` / `(External)` / untagged | `false` |

Reserved orders execute unattended but **a human set the condition**, so they are not "system". For `false`, the server inherits the previous classification for that symbol — but **never inherits `system`**, since an older version stored all bot records that way and inheriting would make that contamination permanent.

### Verifying the integration

Unit tests mock HTTP, so they cannot prove the server actually received anything. The E2E tool closes that gap.

```sh
python tools/journal_sync_e2e.py             # send and verify (leaves data on the server)
python tools/journal_sync_e2e.py --cleanup   # verify, then delete from the server
```

It drives synthetic fills through the production path — `insert_trade → queue → batch send → read back → field comparison`. It uses a temporary DB and always sends `isSimulated=True`, so neither your production DB nor real-trade statistics are affected.

> Repeating without `--cleanup` accumulates test data and skews holdings. The tool reports the leftover count when that happens.


## 14. License

This project is licensed under the Apache License 2.0.
