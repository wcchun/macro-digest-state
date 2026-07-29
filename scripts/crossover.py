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
    """Technical data serves both the digest tickers and the wheel candidates."""
    data = json.loads((REPO_ROOT / "watchlist.json").read_text())
    return [
        sym for sym, flags in data["tickers"].items()
        if flags.get("technicals") or flags.get("wheel")
    ]


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


def fundamentals(tk, close, today):
    """Wheel hard gates 2 and 8, plus the CC leg's ex-dividend check.

    yfinance's .info is slow and occasionally incomplete; every field degrades
    to None rather than failing the run. None means UNRESOLVED to the wheel
    framework, never "passed".
    """
    out = {
        "market_cap": None,
        "free_cash_flow": None,
        "total_cash": None,
        "total_debt": None,
        "net_cash": None,
        "balance_sheet_ok": None,   # gate 8: positive FCF OR net cash
        "dividend_yield_pct": None,
        "ex_dividend_date": None,
        "ex_dividend_is_future": None,
        "sector": None,             # portfolio rule: max 40% per GICS sector
        "quote_type": None,         # EQUITY / ETF / ...
        "is_etf": None,
        "total_assets": None,       # ETFs report AUM, not market cap
    }
    try:
        info = tk.info or {}
    except Exception:
        return out

    def num(key):
        v = info.get(key)
        try:
            v = float(v)
            return None if math.isnan(v) or math.isinf(v) else v
        except (TypeError, ValueError):
            return None

    # Funds have no marketCap. Wheel gate 2 admits "market cap >= 10B OR a
    # broad-based ETF", so the snapshot must say which kind of instrument this
    # is — otherwise a null market cap reads as a gate failure for every ETF.
    qt = info.get("quoteType") or info.get("typeDisp")
    out["quote_type"] = qt
    out["is_etf"] = (str(qt).upper() == "ETF") if qt else None
    out["total_assets"] = num("totalAssets")

    out["market_cap"] = num("marketCap")
    out["free_cash_flow"] = num("freeCashflow")
    out["total_cash"] = num("totalCash")
    out["total_debt"] = num("totalDebt")
    if out["total_cash"] is not None and out["total_debt"] is not None:
        out["net_cash"] = out["total_cash"] - out["total_debt"]

    fcf_ok = out["free_cash_flow"] is not None and out["free_cash_flow"] > 0
    cash_ok = out["net_cash"] is not None and out["net_cash"] > 0
    if out["free_cash_flow"] is not None or out["net_cash"] is not None:
        out["balance_sheet_ok"] = bool(fcf_ok or cash_ok)

    # Derive the yield from the annual dividend rate and our own close: yfinance's
    # dividendYield field is a fraction on some tickers and a percent on others,
    # and guessing from magnitude turns a 0.32% payer into a 32% one.
    rate = num("dividendRate")
    if rate is not None and close:
        out["dividend_yield_pct"] = clean(rate / close * 100)
    else:
        trailing = num("trailingAnnualDividendYield")  # always a true fraction
        if trailing is not None:
            out["dividend_yield_pct"] = clean(trailing * 100)

    # info's exDividendDate is often the LAST ex-date, not the next one. The
    # covered-call leg cares specifically about an ex-date falling before expiry,
    # so record which side of today it sits on rather than implying it is upcoming.
    ex_div = None
    try:
        cal = tk.calendar
        cal_ex = cal.get("Ex-Dividend Date") if isinstance(cal, dict) else None
        if cal_ex:
            ex_div = str(cal_ex[0] if isinstance(cal_ex, (list, tuple)) else cal_ex)[:10]
    except Exception:
        pass
    if ex_div is None and info.get("exDividendDate"):
        try:
            ex_div = datetime.fromtimestamp(
                float(info["exDividendDate"]), tz=timezone.utc
            ).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            ex_div = None
    if ex_div:
        out["ex_dividend_date"] = ex_div
        out["ex_dividend_is_future"] = ex_div >= today.strftime("%Y-%m-%d")

    out["sector"] = info.get("sector") or None
    return out


def analyse(symbol):
    tk = yf.Ticker(symbol)
    df = tk.history(period="1y")
    if df.empty:
        return {"error": "no price data"}

    # Yahoo intermittently returns a trailing row with volume but NaN OHLC (a
    # partially-formed bar). Taking .iloc[-1] off that row nulls out price, every
    # MA, ATR and HV while leaving volume looking fine — so drop unpriced rows
    # before anything reads the tail.
    df = df[df["Close"].notna()]
    if df.empty:
        return {"error": "no priced sessions in window"}

    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA50"] = df["Close"].rolling(50).mean()
    df["MA200"] = df["Close"].rolling(200).mean()

    # True range / ATR20
    prev_close = df["Close"].shift(1)
    tr = (df["High"] - df["Low"]).combine((df["High"] - prev_close).abs(), max)
    tr = tr.combine((df["Low"] - prev_close).abs(), max)
    df["ATR20"] = tr.rolling(20).mean()
    df["ADV20"] = df["Volume"].rolling(20).mean()

    # 20-day realised (historical) volatility, annualised — wheel gate 7 compares
    # this against IV30 from options-result.json. Both must be annualised percents.
    log_ret = (df["Close"] / df["Close"].shift(1)).apply(lambda x: math.log(x) if x > 0 else float("nan"))
    hv20 = log_ret.rolling(20).std() * math.sqrt(252) * 100

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

    result = {
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
        "hv20_pct": clean(hv20.iloc[-1]),
        "adv20_shares": int(adv) if clean(adv, 0) is not None else None,
        "volume_today": int(volume),
        "volume_vs_adv20": clean(volume / adv) if clean(adv) not in (None, 0) else None,
        "next_earnings_date": next_earnings_date(tk),
    }
    result.update(fundamentals(tk, close, datetime.now(timezone.utc).date()))
    return result


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
