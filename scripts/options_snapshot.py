#!/usr/bin/env python3
"""Daily options-positioning snapshot for the watchlist -> options-result.json.

Run by .github/workflows/options.yml after each US close. Reads
watchlist.json (tickers with "technicals": true) and writes, per ticker,
the numbers the Investment Analysis frameworks actually consume:

- ATM IV per expiry (first 6 expirations) -> IV term structure
- term_structure_ratio (front ATM IV / interpolated ~30d ATM IV) — the
  "ramp fuel" gate in the IV Ramp Harvest framework
- chain-wide Volume P/C and OI P/C (options sentiment Signal 2)
- skew proxy: OTM put IV minus OTM call IV on the ~30d expiry (Signal 3)
- max pain for the nearest expiry (Signal 7)
- top-5 open-interest strikes each side (kept from the original workflow)
- earnings-within-30-days flag

It also appends each session's ~30d ATM IV to iv-history.json (capped at
252 rows per ticker) and reports iv_rank_to_date — free sources don't
publish IVR, so this file gradually builds the 52-week IV rank that the
straddle and sentiment frameworks gate on. The rank is only meaningful
once sample_size grows; always check sample_size before trusting it.
"""

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "options-result.json"
IV_HISTORY_FILE = REPO_ROOT / "iv-history.json"
MAX_EXPIRIES = 6
IV_HISTORY_CAP = 252  # one trading year


def clean(value, digits=4):
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


def iv_at_strike(chain_side, strike_target):
    """IV of the contract whose strike is nearest the target; None if junk."""
    df = chain_side[chain_side["impliedVolatility"] > 0.01]
    if df.empty:
        return None
    row = df.iloc[(df["strike"] - strike_target).abs().argsort().iloc[0]]
    return float(row["impliedVolatility"])


def atm_iv(chain, spot):
    """Average of call and put IV at the strike nearest spot."""
    call_iv = iv_at_strike(chain.calls, spot)
    put_iv = iv_at_strike(chain.puts, spot)
    ivs = [iv for iv in (call_iv, put_iv) if iv is not None]
    return sum(ivs) / len(ivs) if ivs else None


def interpolate_iv30(term_structure):
    """Linear interpolation of ATM IV at 30 DTE; nearest expiry if no bracket."""
    points = [(p["dte"], p["atm_iv"]) for p in term_structure if p["atm_iv"] is not None]
    if not points:
        return None
    points.sort()
    below = [p for p in points if p[0] <= 30]
    above = [p for p in points if p[0] > 30]
    if below and above:
        (d1, v1), (d2, v2) = below[-1], above[0]
        if d2 == d1:
            return v1
        return v1 + (v2 - v1) * (30 - d1) / (d2 - d1)
    return (below or above)[-1 if below else 0][1]


def max_pain(chain):
    """Strike minimising total intrinsic payout by option writers at expiry."""
    strikes = sorted(set(chain.calls["strike"]) | set(chain.puts["strike"]))
    if not strikes:
        return None
    calls = chain.calls[["strike", "openInterest"]].fillna(0)
    puts = chain.puts[["strike", "openInterest"]].fillna(0)
    best_strike, best_cost = None, None
    for s in strikes:
        call_cost = ((s - calls["strike"]).clip(lower=0) * calls["openInterest"]).sum()
        put_cost = ((puts["strike"] - s).clip(lower=0) * puts["openInterest"]).sum()
        total = call_cost + put_cost
        if best_cost is None or total < best_cost:
            best_strike, best_cost = s, total
    return clean(best_strike, 2)


def top5_oi(df):
    cols = ["strike", "lastPrice", "bid", "ask", "volume", "openInterest", "impliedVolatility"]
    top = df.nlargest(5, "openInterest")[cols].copy()
    top["impliedVolatility"] = top["impliedVolatility"].round(4)
    records = top.to_dict(orient="records")
    for rec in records:
        for k, v in rec.items():
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                rec[k] = None
    return records


def earnings_within_30d(tk, today):
    try:
        cal = tk.calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        for d in dates or []:
            delta = (datetime.strptime(str(d)[:10], "%Y-%m-%d").date() - today).days
            if 0 <= delta <= 30:
                return {"flag": True, "date": str(d)[:10], "days_away": delta}
    except Exception:
        pass
    return {"flag": False, "date": None, "days_away": None}


def analyse(symbol, today):
    tk = yf.Ticker(symbol)
    hist = tk.history(period="5d")
    if hist.empty:
        return {"error": "no price data"}
    spot = float(hist["Close"].iloc[-1])

    expirations = list(tk.options)[:MAX_EXPIRIES]
    if not expirations:
        return {"error": "no options data"}

    term_structure = []
    total_call_vol = total_put_vol = total_call_oi = total_put_oi = 0.0
    chains = {}
    for exp in expirations:
        chain = tk.option_chain(exp)
        chains[exp] = chain
        dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        term_structure.append({
            "expiration": exp,
            "dte": dte,
            "atm_iv": clean(atm_iv(chain, spot)),
        })
        total_call_vol += float(chain.calls["volume"].fillna(0).sum())
        total_put_vol += float(chain.puts["volume"].fillna(0).sum())
        total_call_oi += float(chain.calls["openInterest"].fillna(0).sum())
        total_put_oi += float(chain.puts["openInterest"].fillna(0).sum())

    iv30 = interpolate_iv30(term_structure)
    front = term_structure[0]
    ts_ratio = None
    if iv30 and front["atm_iv"]:
        ts_ratio = clean(front["atm_iv"] / iv30, 3)

    # Skew proxy on the expiry nearest 30 DTE: IV(95% strike put) - IV(105% strike call)
    skew = None
    near30 = min(
        (p for p in term_structure if p["atm_iv"] is not None),
        key=lambda p: abs(p["dte"] - 30),
        default=None,
    )
    if near30:
        chain = chains[near30["expiration"]]
        put_iv = iv_at_strike(chain.puts, spot * 0.95)
        call_iv = iv_at_strike(chain.calls, spot * 1.05)
        if put_iv is not None and call_iv is not None:
            skew = clean((put_iv - call_iv) * 100, 2)  # vol points

    nearest = chains[expirations[0]]
    return {
        "as_of": hist.index[-1].strftime("%Y-%m-%d"),
        "spot": clean(spot, 2),
        "iv_term_structure": term_structure,
        "iv30_interpolated": clean(iv30),
        "term_structure_ratio": ts_ratio,
        "volume_put_call_ratio": clean(total_put_vol / total_call_vol, 3) if total_call_vol else None,
        "oi_put_call_ratio": clean(total_put_oi / total_call_oi, 3) if total_call_oi else None,
        "skew_25d_proxy_volpts": skew,
        "skew_expiry_used": near30["expiration"] if near30 else None,
        "max_pain_nearest_expiry": max_pain(nearest),
        "nearest_expiration": expirations[0],
        "calls_top5_oi": top5_oi(nearest.calls),
        "puts_top5_oi": top5_oi(nearest.puts),
        "earnings_within_30d": earnings_within_30d(tk, today),
    }


def update_iv_history(results, today):
    """Append today's ~30d ATM IV per ticker; compute rank over stored history."""
    history = {}
    if IV_HISTORY_FILE.exists():
        history = json.loads(IV_HISTORY_FILE.read_text())

    for symbol, data in results.items():
        iv30 = data.get("iv30_interpolated")
        if iv30 is None:
            continue
        rows = history.setdefault(symbol, [])
        entry = {"date": str(today), "iv30": iv30}
        if rows and rows[-1]["date"] == str(today):
            rows[-1] = entry  # same-day re-run: overwrite, don't duplicate
        else:
            rows.append(entry)
        history[symbol] = rows[-IV_HISTORY_CAP:]

        ivs = [r["iv30"] for r in history[symbol]]
        lo, hi = min(ivs), max(ivs)
        rank = clean((iv30 - lo) / (hi - lo) * 100, 1) if hi > lo else None
        data["iv_rank_to_date"] = {
            "rank_pct": rank,
            "sample_size": len(ivs),
            "note": "rank within stored history only; needs ~252 samples to equal a true 52-week IVR",
        }

    IV_HISTORY_FILE.write_text(json.dumps(history, indent=2, allow_nan=False) + "\n")


def main():
    today = datetime.now(timezone.utc).date()
    results = {}
    for symbol in load_watchlist():
        try:
            results[symbol] = analyse(symbol, today)
        except Exception as exc:
            results[symbol] = {"error": str(exc)}
        print(f"{symbol}: ok" if "error" not in results[symbol] else f"{symbol}: {results[symbol]['error']}")

    update_iv_history(results, today)

    output = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }
    OUTPUT_FILE.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(f"Wrote {OUTPUT_FILE} and {IV_HISTORY_FILE}")


if __name__ == "__main__":
    main()
