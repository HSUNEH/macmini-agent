#!/usr/bin/env python3
"""Fetch the latest CBOE VIX close for the finance-news pre-step.

The CBOE historical CSV is updated on trading days.  Keeping its as-of date in
the payload makes a weekend or US-market-holiday value unambiguous.
"""
from __future__ import annotations

import csv
import io
import json
import urllib.request


URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"


def main() -> int:
    try:
        request = urllib.request.Request(URL, headers={"User-Agent": "macmini-agent/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:  # nosec B310 - fixed CBOE URL
            rows = list(csv.DictReader(io.StringIO(response.read().decode("utf-8-sig"))))
        rows = [row for row in rows if row.get("DATE") and row.get("CLOSE")]
        if len(rows) < 2:
            raise ValueError("CBOE VIX history has fewer than two closing values")
        latest, previous = rows[-1], rows[-2]
        close, previous_close = float(latest["CLOSE"]), float(previous["CLOSE"])
        change = close - previous_close
        print(json.dumps({
            "vix": {
                "name": "CBOE Volatility Index (VIX)",
                "as_of": latest["DATE"],
                "close": close,
                "previous_close": previous_close,
                "change": round(change, 2),
                "change_percent": round(change / previous_close * 100, 2) if previous_close else None,
                "source": "CBOE",
                "source_url": URL,
            }
        }, ensure_ascii=False))
    except Exception as exc:
        # A temporary data-source failure must not suppress the complete digest.
        print(json.dumps({"vix": None, "error": f"{exc.__class__.__name__}: {exc}"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
