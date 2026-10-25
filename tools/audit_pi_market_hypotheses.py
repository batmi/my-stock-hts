"""Pi 체결의 시장 환경을 공식 일봉으로 대조한다(표준 라이브러리, 읽기 전용).

운영 config/앱을 import 하지 않는다. 입력 거래는 audit_pi_trade_evidence.py 가
Pi 원장·로그에서 만든 episodes JSON만 쓴다. 지수·종목 일봉은 지정한 KRX 저장소다.
기본 판정은 매수 시각에 이미 확정된 직전 거래일 종가이고, 같은 날 종가는 사후
민감도일 뿐 주문 가능했던 신호로 취급하지 않는다. 시장 필터 산식은
core.indicators.get_market_filter_blocked 의 SMA ± band 히스테리시스와 같다.

필터가 가린 실제 거래 손익은 조건부 집계이지 포트폴리오 반사실 수익이 아니다.
진입·증액을 막으면 이후 수량·평단·청산·슬롯·새 진입이 바뀐다. Pi 당시 설정
파일은 입력에 없으므로 MA 기간은 확인된 운용값이라고 주장하지 않는다.

예: python3 tools/audit_pi_market_hypotheses.py \
      --episodes tmp/pi_hypotheses/episodes.json \
      --output tmp/pi_hypotheses/market.json
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter, deque
import json
import math
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[1]
MARKETS = {"KOSPI": ("kospi_dd_trd", "코스피"), "KOSDAQ": ("kosdaq_dd_trd", "코스닥")}


def read_only(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def date_key(value):
    return str(value)[:10].replace("-", "")


def filter_series(rows, period, band_pct):
    """No future inputs. Unknown SMA windows retain state and are marked not ready."""
    out, window, state, warmed = [], deque(), False, False
    for row in rows:
        value = row["close"]
        window.append(value)
        if len(window) > period:
            window.popleft()
        ready = len(window) == period and all(v is not None and math.isfinite(v) for v in window)
        sma = sum(window) / period if ready else None
        if ready:
            warmed = True
            if value < sma * (1.0 - band_pct / 100.0):
                state = True
            elif value > sma * (1.0 + band_pct / 100.0):
                state = False
        out.append({"date": row["date"], "close": value, "sma": sma,
                    "gap_pct": (value / sma - 1) * 100 if sma else None,
                    "ready": ready, "blocked": state if warmed else False})
    return out


def lookup(series, day, previous=True):
    days = [r["date"] for r in series]
    at = (bisect.bisect_left(days, day) if previous else bisect.bisect_right(days, day)) - 1
    return series[at] if at >= 0 else None


def summarize_market(rows, filters, start, end):
    before = lookup(rows, start, True)
    sample = [r for r in rows if start <= r["date"] <= end]
    if not sample:
        return {"bars": 0}
    last = sample[-1]
    high = before["close"] if before else sample[0]["close"]
    mdd = 0.0
    for row in sample:
        high = max(high, row["close"])
        mdd = min(mdd, (row["close"] / high - 1) * 100)
    summaries = {}
    for period, series in filters.items():
        relevant = [r for r in series if start <= r["date"] <= end and r["ready"]]
        summaries[str(period)] = {
            "ready_days": len(relevant), "blocked_days": sum(r["blocked"] for r in relevant),
            "state_changes": sum(a["blocked"] != b["blocked"] for a, b in zip(relevant, relevant[1:])),
        }
    return {"bars": len(sample), "baseline_date": before["date"] if before else sample[0]["date"],
            "baseline_close": before["close"] if before else sample[0]["close"],
            "last_date": last["date"], "last_close": last["close"],
            "return_pct": (last["close"] / (before or sample[0])["close"] - 1) * 100,
            "close_to_close_mdd_pct": mdd, "filter": summaries}


def stock_features(rows, index_rows, day):
    """Pre-entry confirmed close only; simple candidate gates, not the live score."""
    past = [r for r in rows if r["date"] < day][-260:]
    if len(past) < 61:
        return {"ready": False, "bars": len(past)}
    closes = [r["close"] for r in past]
    if any(v is None or not math.isfinite(v) or v <= 0 for v in closes):
        return {"ready": False, "bars": len(past), "reason": "invalid_close"}
    ema20 = ema60 = closes[0]
    for price in closes[1:]:
        ema20 = price * (2 / 21) + ema20 * (1 - 2 / 21)
        ema60 = price * (2 / 61) + ema60 * (1 - 2 / 61)
    latest = past[-1]
    ret20 = (closes[-1] / closes[-21] - 1) * 100
    idx_start = lookup(index_rows, past[-21]["date"], False)
    idx_end = lookup(index_rows, latest["date"], False)
    idx_ret = (idx_end["close"] / idx_start["close"] - 1) * 100 if idx_start and idx_end else None
    # The supplemental store has raw OHLC. Large share changes flag corporate action
    # contamination rather than silently treating it as genuine momentum.
    changes = []
    for a, b in zip(past[-61:], past[-60:]):
        if a.get("shares") and b.get("shares") and abs(b["shares"] / a["shares"] - 1) > 0.05:
            changes.append(b["date"])
    return {"ready": True, "date": latest["date"], "close": closes[-1], "ema20": ema20,
            "ema60": ema60, "close_above_ema60": closes[-1] > ema60,
            "ema20_above_ema60": ema20 >= ema60,
            "trend_aligned": closes[-1] > ema60 and ema20 >= ema60,
            "return_20_bars_pct": ret20, "index_return_same_dates_pct": idx_ret,
            "relative_return_20_bars_pp": ret20 - idx_ret if idx_ret is not None else None,
            "positive_relative_return_20": ret20 > idx_ret if idx_ret is not None else None,
            "raw_price_share_change_flags": changes}


def actual_group(episodes):
    closed = [e for e in episodes if e.get("end")]
    automatic = [e for e in closed if e.get("exit_is_auto") is True]
    return {"episodes": len(episodes), "closed": len(closed), "open": len(episodes) - len(closed),
            "actual_closed_net": sum(e["actual_net_profit"] for e in closed),
            "automatic_closed": len(automatic),
            "automatic_closed_net": sum(e["actual_net_profit"] for e in automatic),
            "positive_closed": sum(e["actual_net_profit"] > 0 for e in closed),
            "episode_ids": [e["id"] for e in episodes]}


def exposure_benchmark(episodes, recorded_equity, stocks, index, start, end, seed):
    """Actual close equity versus prior-actual-exposure-matched index (descriptive)."""
    fills = sorted((fill for e in episodes for fill in e["buys"] + e["sells"]),
                   key=lambda fill: (fill["time"], fill["id"]))
    cash, holdings, at = seed, Counter(), 0
    previous_exposure, benchmark, high, benchmark_mdd = 0.0, seed, seed, 0.0
    previous_index = lookup(index, start, True)["close"]
    daily, discrepancies = [], []
    stored_by_date = {date_key(row["date"]): row for row in recorded_equity}
    for row in index:
        day = row["date"]
        if day < start or day > end:
            continue
        index_return = row["close"] / previous_index - 1
        benchmark *= 1 + previous_exposure * index_return
        high = max(high, benchmark)
        benchmark_mdd = min(benchmark_mdd, (benchmark / high - 1) * 100)
        while at < len(fills) and date_key(fills[at]["time"]) <= day:
            fill = fills[at]
            if fill["type"] == "매수":
                holdings[fill["code"]] += fill["qty"]
                cash -= fill["amount"] + fill["fee"]
            elif fill["type"] == "매도":
                holdings[fill["code"]] -= fill["qty"]
                cash += fill["amount"] - fill["fee"]
            else:
                raise ValueError(f"Unknown fill side: {fill['type']}")
            if holdings[fill["code"]] < 0:
                raise ValueError("Negative reconstructed position")
            at += 1
        marked, prices = 0.0, {}
        for code, qty in holdings.items():
            if not qty:
                continue
            stock = lookup(stocks[code], day, False)
            if stock is None or stock["date"] != day:
                raise ValueError(f"Official close missing for held stock {code} on {day}")
            prices[code] = {"qty": qty, "close": stock["close"]}
            marked += qty * stock["close"]
        equity = cash + marked
        exposure = marked / equity
        daily.append({"date": day, "cash": cash, "stock_value": marked, "actual_close_equity": equity,
                      "close_exposure": exposure, "benchmark_applied_exposure": previous_exposure,
                      "index_daily_return": index_return, "matched_benchmark_equity": benchmark,
                      "positions": prices})
        recorded = stored_by_date.get(day)
        if recorded and abs(equity - recorded["total"]) > 0.01:
            discrepancies.append({"date": day, "reconstructed": equity,
                                  "recorded": recorded["total"], "difference": equity - recorded["total"]})
        previous_exposure, previous_index = exposure, row["close"]
    actual_high, actual_mdd = seed, 0.0
    for row in daily:
        actual_high = max(actual_high, row["actual_close_equity"])
        actual_mdd = min(actual_mdd, (row["actual_close_equity"] / actual_high - 1) * 100)
    return {
        "method": "index daily return times previous actual end-of-day stock/equity exposure; "
                  "cash earns zero; benchmark has no turnover cost; descriptive comparison, not alpha or causal attribution",
        "seed": seed, "first_day_previous_exposure": 0.0,
        "actual_final_equity": daily[-1]["actual_close_equity"],
        "actual_return_pct": (daily[-1]["actual_close_equity"] / seed - 1) * 100,
        "actual_close_mdd_pct": actual_mdd,
        "matched_index_final_equity": benchmark, "matched_index_return_pct": (benchmark / seed - 1) * 100,
        "matched_index_mdd_pct": benchmark_mdd,
        "mean_close_exposure_pct": sum(r["close_exposure"] for r in daily) / len(daily) * 100,
        "max_close_exposure_pct": max(r["close_exposure"] for r in daily) * 100,
        "actual_fees": sum(f["fee"] for f in fills),
        "actual_gross_buy_notional": sum(f["amount"] for e in episodes for f in e["buys"]),
        "recorded_equity_discrepancies": discrepancies, "daily": daily,
    }


def conditional_pyramid_attribution(episodes, events, periods):
    """Remove market-blocked adds, freeze each actual liquidation date/price."""
    by_episode = {}
    for event in events:
        by_episode.setdefault(event["episode_id"], []).append(event)
    result = {}
    for period in periods:
        changed = []
        for episode in episodes:
            if not episode.get("end") or len(episode["buys"]) < 2:
                continue
            if len(episode["sells"]) != 1:
                raise ValueError("Frozen exit attribution currently requires one full liquidation")
            keep, omitted = [episode["buys"][0]], []
            for buy, event in zip(episode["buys"][1:], by_episode[episode["id"]][1:]):
                state = event["filters"][str(period)]["previous_close"]
                if state is None or not state["ready"]:
                    raise ValueError("Unknown market status in pyramid attribution")
                (omitted if state["blocked"] else keep).append(buy)
            qty = sum(b["qty"] for b in keep)
            cost = sum(b["amount"] + b["fee"] for b in keep)
            sale = episode["sells"][0]
            amount = qty * sale["price"]
            # This fee matches every Pi sell fill (metadata validates the rate).
            hypothetical_net = amount - int(amount * 0.002177) - cost
            actual_net = episode["cash_flow_net_profit"]
            changed.append({"episode_id": episode["id"], "name": episode["name"],
                            "omitted_adds": len(omitted), "omitted_notional": sum(b["amount"] for b in omitted),
                            "actual_cashflow_net": actual_net,
                            "frozen_exit_no_blocked_adds_net": hypothetical_net,
                            "difference": hypothetical_net - actual_net})
        result[str(period)] = {
            "omitted_adds": sum(r["omitted_adds"] for r in changed),
            "difference": sum(r["difference"] for r in changed), "episodes": changed,
            "limitation": "Liquidation prices/dates frozen to actual schedule. Removing adds changes average cost, "
                          "risk and exit timing; this is attribution, not an executable replay or expected improvement.",
        }
    return result


def build_report(dataset, store, periods=(20, 60, 80), band_pct=1.0):
    episodes = dataset["episodes"]
    start = min(date_key(e["start"]) for e in episodes)
    end = max(date_key(x["date"]) for x in dataset["equity"])
    index_data, stock_data = {}, {}
    with read_only(store) as con:
        for market, (api, name) in MARKETS.items():
            index_data[market] = [dict(r) for r in con.execute(
                "SELECT bas_dd AS date,close FROM index_daily WHERE api_id=? AND name=? "
                "AND bas_dd<=? ORDER BY bas_dd", (api, name, end))]
        codes = sorted({e["code"] for e in episodes})
        for code in codes:
            stock_data[code] = [dict(r) for r in con.execute(
                "SELECT bas_dd AS date,market,close,shares FROM stock_daily "
                "WHERE bas_dd>=? AND bas_dd<=? AND code=? ORDER BY bas_dd",
                (str(int(start[:4]) - 2) + start[4:], end, code))]
    filters = {m: {p: filter_series(rows, p, band_pct) for p in periods} for m, rows in index_data.items()}
    events, first_by_episode = [], {}
    for episode in episodes:
        for n, buy in enumerate(episode["buys"]):
            day = date_key(buy["time"])
            stock_row = lookup(stock_data[episode["code"]], day, True)
            market = stock_row["market"] if stock_row else None
            if market == "KOSDAQ GLOBAL":
                market = "KOSDAQ"
            event = {"episode_id": episode["id"], "code": episode["code"], "name": episode["name"],
                     "time": buy["time"], "kind": "entry" if n == 0 else "pyramid",
                     "buy_number": n + 1, "qty": buy["qty"], "price": buy["price"],
                     "market": market, "market_assignment_date": stock_row["date"] if stock_row else None,
                     "filters": {}, "previous_close_stock": stock_features(
                         stock_data[episode["code"]], index_data.get(market, []), day)}
            for period in periods:
                series = filters.get(market, {}).get(period, [])
                event["filters"][str(period)] = {
                    "previous_close": lookup(series, day, True),
                    "same_day_close_sensitivity": lookup(series, day, False),
                }
            events.append(event)
            if n == 0:
                first_by_episode[episode["id"]] = event

    cohorts = {}
    for period in periods:
        period_key = str(period)
        cohorts[period_key] = {}
        for timing in ("previous_close", "same_day_close_sensitivity"):
            cohort = {}
            for blocked in (False, True, None):
                key = "unknown" if blocked is None else "blocked" if blocked else "allowed"
                def category(event):
                    state = event["filters"][period_key][timing]
                    return state["blocked"] if state and state["ready"] else None
                selected = [e for e in episodes if category(first_by_episode[e["id"]]) is blocked]
                stat = actual_group(selected)
                matching_events = [e for e in events if category(e) is blocked]
                stat["buy_event_counts"] = dict(Counter(e["kind"] for e in matching_events))
                stat["buy_notional"] = sum(e["qty"] * e["price"] for e in matching_events)
                cohort[key] = stat
            cohorts[period_key][timing] = cohort

    trend_cohorts = {}
    for gate in ("trend_aligned", "close_above_ema60", "ema20_above_ema60", "positive_relative_return_20"):
        trend_cohorts[gate] = {}
        for passed in (True, False, None):
            selected = [e for e in episodes if first_by_episode[e["id"]]["previous_close_stock"].get(gate) is passed]
            trend_cohorts[gate]["unknown" if passed is None else "passed" if passed else "failed"] = actual_group(selected)
    return {
        "provenance": {"episodes": dataset["meta"], "supplemental_official_price_db": str(store),
                       "interval_start": start, "interval_end": end,
                       "index_coverage": {m: [r[0]["date"], r[-1]["date"], len(r)] for m, r in index_data.items()},
                       "mac_trading_data_used": False, "operating_config_imported": False},
        "assumptions": {"market_filter_periods": list(periods), "band_pct": band_pct,
                        "bear_release": False, "actual_pi_filter_enabled": False,
                        "period_and_band_verified_against_pi_config": False,
                        "primary_timing": "previous confirmed trading-day close",
                        "same_day_close": "hindsight sensitivity, not an executable intraday signal",
                        "equity_effect": "actual conditional P&L only; no portfolio counterfactual",
                        "stock_gate": "new candidate diagnostic EMA20/60 on last 260 confirmed raw daily closes; not full score",
                        "incomplete_intraday_index": "actual intraday market state cannot be recovered exactly from daily closes"},
        "market": {m: summarize_market(r, filters[m], start, end) for m, r in index_data.items()},
        "events": events, "entry_conditioned_actual_pnl": cohorts,
        "previous_close_trend_cohorts": trend_cohorts,
        "exposure_matched_index": exposure_benchmark(
            episodes, dataset["equity"], stock_data, index_data["KOSPI"], start, end,
            float(dataset["meta"]["state"]["seed"])),
        "conditional_pyramid_attribution": conditional_pyramid_attribution(episodes, events, periods),
        "replay_limitations": [
            "No Pi historical config snapshot or full historical watchlist is supplied.",
            "Pi fills/logs permit actual-entry path analysis, not all missed alternative entries.",
            "A daily portfolio replay cannot reproduce intraday score, order-book gates or 60-second fills exactly.",
            "Raw supplemental stock prices can contain corporate actions; share-change flags are included.",
        ],
    }


def print_report(report):
    p = report["provenance"]
    print(f"Pi market hypotheses {p['interval_start']}..{p['interval_end']} (KRW)")
    print("Primary: previous confirmed close. Same-day close is HINDSIGHT sensitivity.")
    print("MA periods/band are explicit scenarios; actual Pi MA/band config is unavailable.")
    for market, stat in report["market"].items():
        print(f"{market}: {stat['bars']} days, return {stat['return_pct']:+.2f}%, "
              f"close MDD {stat['close_to_close_mdd_pct']:.2f}%")
        for period, counts in stat["filter"].items():
            print(f"  SMA{period}: blocked {counts['blocked_days']}/{counts['ready_days']}, "
                  f"state changes {counts['state_changes']}")
    print("\nPeriod timing                     blocked entry/add  allowed entry/add  actual closed net(blocked/allowed)")
    for period, timings in report["entry_conditioned_actual_pnl"].items():
        for timing, cohort in timings.items():
            blocked, allowed = cohort["blocked"], cohort["allowed"]
            b, a = blocked["buy_event_counts"], allowed["buy_event_counts"]
            print(f"{period:>6} {timing:<27} {b.get('entry',0):>2}/{b.get('pyramid',0):<2} "
                  f"{a.get('entry',0):>17}/{a.get('pyramid',0):<2} "
                  f"{blocked['actual_closed_net']:>14,.0f}/{allowed['actual_closed_net']:,.0f}")
    print("\nPrevious-close stock trend cohorts (actual P&L; NOT effect of applying a filter)")
    for gate, groups in report["previous_close_trend_cohorts"].items():
        print(gate, " | ".join(f"{key}: {value['closed']} closed, {value['actual_closed_net']:+,.0f} "
                              f"({value['open']} open)" for key, value in groups.items()))
    benchmark = report["exposure_matched_index"]
    print(f"\nActual official-close return {benchmark['actual_return_pct']:+.2f}% vs "
          f"exposure-matched KOSPI {benchmark['matched_index_return_pct']:+.2f}% "
          f"(benchmark no turnover costs; descriptive only)")
    print(f"Mean/max closing exposure {benchmark['mean_close_exposure_pct']:.2f}%/"
          f"{benchmark['max_close_exposure_pct']:.2f}%; actual fees {benchmark['actual_fees']:,.0f}")
    print("Frozen actual-exit schedule attribution, not a tradable counterfactual:")
    for period, stat in report["conditional_pyramid_attribution"].items():
        print(f"  SMA{period} excludes {stat['omitted_adds']} adds; cashflow difference {stat['difference']:+,.0f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes", type=Path, default=ROOT / "tmp/pi_hypotheses/episodes.json")
    parser.add_argument("--store", type=Path, default=ROOT / "data/krx_openapi.db")
    parser.add_argument("--periods", default="20,60,80")
    parser.add_argument("--band", type=float, default=1.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    periods = tuple(int(s) for s in args.periods.split(","))
    if not periods or any(p <= 0 for p in periods) or not 0 <= args.band < 100:
        parser.error("periods must be positive and band must be in [0,100)")
    with args.episodes.open() as source:
        dataset = json.load(source)
    report = build_report(dataset, args.store, periods, args.band)
    print_report(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        print(f"JSON: {args.output}")


if __name__ == "__main__":
    main()
