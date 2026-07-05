#!/usr/bin/env python3
"""Daily technicals snapshot for the watchlist -> crossover-result.json.

Run by .github/workflows/crossover.yml after each US session. Reads
watchlist.json (tickers with "technicals": true) and writes, per ticker:
moving averages, TRUE golden/death-cross detection (sign flip of the
20-50 spread, not just current MA state), ATR, volume vs ADV, and the
next earnings date when yfinance knows it.

Consumers: the Stock News Digest routine (technicals snapshot + strategy
triage) and the /volume and /straddle analysis skills.
"""

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "crossover-result.json"


def clean(value, digits=2):
    """Round to digits; map NaN/inf (e.g. 200MA on a young listing) to None."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return round(value, digits)


def load_watchlist():
    data = json.loads((REPO_ROOT / "watchlist.json").read_text())
    return [sym for sym, flags in data["tickers"].items() if flags.get("technicals")]


def next_earnings_date(tk):
    """Best effort — yfinance's calendar coverage is spotty; None is fine."""
    try:
        cal = tk.calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if dates:
            future = [d for d in dates if str(d) >= datetime.now(timezone.utc).strftime("%Y-%m-%d")]
            if future:
                return str(min(future))
            return str(dates[0])
    except Exception:
        pass
    return None


def analyse(symbol):
    tk = yf.Ticker(symbol)
    df = tk.history(period="1y")
    if df.empty:
        return {"error": "no price data"}

    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA50"] = df["Close"].rolling(50).mean()
    df["MA200"] = df["Close"].rolling(200).mean()

    # True range / ATR20
    prev_close = df["Close"].shift(1)
    tr = (df["High"] - df["Low"]).combine((df["High"] - prev_close).abs(), max)
    tr = tr.combine((df["Low"] - prev_close).abs(), max)
    df["ATR20"] = tr.rolling(20).mean()
    df["ADV20"] = df["Volume"].rolling(20).mean()

    close = float(df["Close"].iloc[-1])
    ma20 = df["MA20"].iloc[-1]
    ma50 = df["MA50"].iloc[-1]
    ma200 = df["MA200"].iloc[-1]
    atr = df["ATR20"].iloc[-1]
    adv = df["ADV20"].iloc[-1]
    volume = float(df["Volume"].iloc[-1])

    # A golden/death cross is the EVENT of the 20-50 spread changing sign,
    # not the standing state 20MA > 50MA. Find the most recent sign flip.
    spread = (df["MA20"] - df["MA50"]).dropna()
    cross_date = None
    cross_direction = None
    days_since_cross = None
    if len(spread) >= 2:
        sign = spread.gt(0)
        flips = sign[sign != sign.shift(1)].index[1:]  # skip the first defined row
        if len(flips) > 0:
            last_flip = flips[-1]
            cross_date = last_flip.strftime("%Y-%m-%d")
            cross_direction = "golden" if sign.loc[last_flip] else "death"
            days_since_cross = int(len(spread.loc[last_flip:]) - 1)  # trading days

    def pct_from(ma):
        if ma is None or (isinstance(ma, float) and math.isnan(ma)) or not ma:
            return None
        try:
            if math.isnan(float(ma)):
                return None
        except (TypeError, ValueError):
            return None
        return clean((close / float(ma) - 1) * 100)

    return {
        "as_of": df.index[-1].strftime("%Y-%m-%d"),
        "close": clean(close),
        "20MA": clean(ma20),
        "50MA": clean(ma50),
        "200MA": clean(ma200),
        "pct_vs_20MA": pct_from(ma20),
        "pct_vs_50MA": pct_from(ma50),
        "pct_vs_200MA": pct_from(ma200),
        "ma_state_bullish": bool(ma20 > ma50) if not (math.isnan(float(ma20)) or math.isnan(float(ma50))) else None,
        "last_cross": {
            "direction": cross_direction,
            "date": cross_date,
            "trading_days_ago": days_since_cross,
        },
        "atr20": clean(atr),
        "atr20_pct": clean(atr / close * 100) if clean(atr) is not None else None,
        "adv20_shares": int(adv) if clean(adv, 0) is not None else None,
        "volume_today": int(volume),
        "volume_vs_adv20": clean(volume / adv) if clean(adv) not in (None, 0) else None,
        "next_earnings_date": next_earnings_date(tk),
    }


def main():
    results = {}
    for symbol in load_watchlist():
        try:
            results[symbol] = analyse(symbol)
        except Exception as exc:  # one bad ticker must not sink the whole run
            results[symbol] = {"error": str(exc)}
        print(f"{symbol}: {json.dumps(results[symbol], default=str)}")

    output = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }
    OUTPUT_FILE.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(f"Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
