"""Read-only KOSPI audit history downloader; never imports operating application code.

Public FinanceData KRX-cache CSVs extend the local official series before 2010.
The local SQLite database is opened mode=ro and wins every overlapping date.
Network access occurs only with --fetch; responses are preserved with SHA256.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import hashlib
import io
import json
import math
from pathlib import Path
import sqlite3
import urllib.error
import urllib.request


BASE_URL = "https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/refs/heads/master/data/index/year_ks11"
DEFAULT_OUTPUT = "data/audit_runs/kospi_dual_filter_20261025"


def parse_csv(raw: bytes, year: int) -> list[dict]:
    rows = []
    for source in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
        day = dt.date.fromisoformat(source["Date"][:10])
        if day.year != year:
            raise ValueError(f"Wrong year {day} in {year}.csv")
        row = {"date": day.strftime("%Y%m%d")}
        for key in ("Open", "High", "Low", "Close", "Volume"):
            value = float(source[key]) if source.get(key) else None
            if value is not None and not math.isfinite(value):
                raise ValueError(f"Non-finite {key} at {day}")
            # Early KRX history has closes but zero/missing OHL; do not invent them.
            if key in ("Open", "High", "Low") and value == 0:
                value = None
            row[key.lower()] = value
        if row["close"] is None or row["close"] <= 0:
            raise ValueError(f"Invalid close at {day}")
        rows.append(row)
    if not rows:
        raise ValueError("Empty CSV")
    if len({row["date"] for row in rows}) != len(rows):
        raise ValueError("Duplicate dates")
    return sorted(rows, key=lambda row: row["date"])


def acquire_year(year: int, output: Path, fetch: bool) -> dict:
    path = output / "raw" / f"fdr_ks11_{year}.csv"
    url = f"{BASE_URL}/{year}.csv"
    try:
        if path.exists():
            raw = path.read_bytes()
            acquisition = "saved_raw"
        elif fetch:
            request = urllib.request.Request(url, headers={"User-Agent": "KOSPI-readonly-audit/1.0"})
            with urllib.request.urlopen(request, timeout=12) as response:
                raw = response.read()
            parse_csv(raw, year)  # Preserve only a valid CSV, never an error page.
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            acquisition = "public_https_download"
        else:
            return {"year": year, "url": url, "status": "not_downloaded"}
        rows = parse_csv(raw, year)
        return {"year": year, "url": url, "path": str(path), "status": "ok",
                "acquisition": acquisition, "sha256": hashlib.sha256(raw).hexdigest(),
                "rows": rows}
    except (OSError, ValueError, KeyError, urllib.error.URLError) as error:
        return {"year": year, "url": url, "status": "failed", "error": str(error)}


def build(db: Path, output: Path, start_year: int, end_year: int, fetch: bool) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        sources = list(pool.map(lambda y: acquire_year(y, output, fetch), range(start_year, end_year + 1)))
    external = {row["date"]: row for source in sources for row in source.get("rows", [])}
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        local = {}
        for value in connection.execute("SELECT bas_dd, open, high, low, close, volume FROM index_daily WHERE api_id='kospi_dd_trd' AND name='코스피' ORDER BY bas_dd"):
            row = dict(value)
            day = row.pop("bas_dd")
            dt.datetime.strptime(day, "%Y%m%d")
            local[day] = {"date": day, **row}
    if not local:
        raise ValueError("Local official KOSPI series is empty")
    first_local, last_local = min(local), max(local)
    overlap = sorted(set(local) & set(external))
    mismatches = []
    field_counts = {key: 0 for key in ("open", "high", "low", "close")}
    for day in overlap:
        differences = {}
        for key in field_counts:
            a, b = local[day][key], external[day][key]
            if a is None or b is None or abs(a - b) > 0.005:
                field_counts[key] += 1
                differences[key] = {"local": a, "external": b}
        if differences:
            mismatches.append({"date": day, "differences": differences})
    merged = [{**row, "source": "FinanceData_KRX_public_cache"} for day, row in external.items() if day < first_local]
    merged += [{**row, "source": "local_KRX_Open_API"} for row in local.values()]
    merged.sort(key=lambda row: row["date"])
    invalid_ohlc = []
    missing_ohlc = []
    for row in merged:
        if any(row[key] is None for key in ("open", "high", "low")):
            missing_ohlc.append(row["date"])
        elif row["low"] > min(row["open"], row["close"], row["high"]) or row["high"] < max(row["open"], row["close"], row["low"]) or min(row[k] for k in ("open", "high", "low")) <= 0:
            invalid_ohlc.append(row["date"])
    saturday = [r["date"] for r in merged if dt.datetime.strptime(r["date"], "%Y%m%d").weekday() == 5]
    sunday = [r["date"] for r in merged if dt.datetime.strptime(r["date"], "%Y%m%d").weekday() == 6]
    first_year = int(merged[0]["date"][:4])
    missing_extension_years = [y for y in range(first_year, int(first_local[:4])) if not any(r["date"].startswith(str(y)) for r in merged)]
    metadata = {
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_kind": "KRX price index; public FinanceData cache before 2010, official local KRX Open API thereafter",
        "database_read_only": True,
        "local_source": str(db),
        "public_cache_documentation": "https://github.com/FinanceData/fdr_krx_data_cache",
        "date_format": "YYYYMMDD",
        "acquisition_note": "Saved raw CSVs may be reconstructed losslessly from the web tool's line-numbered text. All original line indices must be present before saving. SHA256 describes saved CSV bytes, not the HTTP response bytes when reconstructed.",
        "sources": [{key: value for key, value in source.items() if key != "rows"} | ({"count": len(source["rows"]), "first": source["rows"][0]["date"], "last": source["rows"][-1]["date"]} if "rows" in source else {}) for source in sources],
        "period": {"first": merged[0]["date"], "last": merged[-1]["date"], "bars": len(merged), "local_first": first_local, "local_last": last_local, "extension_rows": sum(r["date"] < first_local for r in merged)},
        "overlap": {"count": len(overlap), "first": overlap[0] if overlap else None, "last": overlap[-1] if overlap else None, "mismatch_days": len(mismatches), "mismatch_field_counts": field_counts, "mismatches": mismatches, "interpretation": "Separate distribution paths from the same ultimate KRX source; not economically independent price measurements. Official local rows take precedence."},
        "quality": {"duplicate_dates": len(merged) - len({r["date"] for r in merged}), "invalid_ohlc_dates": invalid_ohlc, "missing_ohlc_count": len(missing_ohlc), "missing_ohlc_first": missing_ohlc[0] if missing_ohlc else None, "missing_ohlc_last": missing_ohlc[-1] if missing_ohlc else None, "saturday_count": len(saturday), "saturday_first": saturday[0] if saturday else None, "saturday_last": saturday[-1] if saturday else None, "sunday_dates": sunday, "missing_extension_years": missing_extension_years},
        "adjustment_note": "This is the published KOSPI price index, not adjusted constituent stock prices or a dividend-reinvested total-return index. No adjustment, interpolation, or synthetic pre-OHLC values are applied. Earlier Saturday observations are retained.",
        "limitations": ["The public mirror may be stale or contain intraday snapshots; overlapping official local rows win.", "Pre-2010 history has not been independently checked against another direct original source.", "Early missing open/high/low values cannot support next-open execution tests; use complete-OHLC subset or explicitly separate close-only tests.", "MA lengths are observation counts; early six-day trading weeks differ from current five-day weeks."]}
    dataset = {"metadata": metadata["period"], "rows": merged}
    payload = json.dumps(dataset, ensure_ascii=False, indent=2) + "\n"
    metadata["dataset_sha256"] = hashlib.sha256(payload.encode()).hexdigest()
    (output / "kospi_extended.json").write_text(payload)
    (output / "extended_source.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/krx_openapi.db"))
    parser.add_argument("--output", type=Path, default=Path(DEFAULT_OUTPUT))
    parser.add_argument("--start-year", type=int, default=1980)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--fetch", action="store_true", help="Read public CSV URLs; never writes the operating DB")
    args = parser.parse_args()
    if not 1900 <= args.start_year <= args.end_year <= dt.date.today().year:
        parser.error("Invalid year range")
    result = build(args.db, args.output, args.start_year, args.end_year, args.fetch)
    print(json.dumps({"period": result["period"], "quality": result["quality"], "successful_files": sum(s["status"] == "ok" for s in result["sources"]), "overlap": {k: v for k, v in result["overlap"].items() if k != "mismatches"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
