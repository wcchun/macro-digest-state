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

# Wheel strategy (Investment Analysis/wheel_strategy_system_prompt.md)
R_FREE = 0.045                       # risk-free rate for Black-Scholes delta
WHEEL_DTE_MIN, WHEEL_DTE_MAX = 30, 45
WHEEL_TARGET_DELTAS = [0.15, 0.20, 0.25, 0.30]  # Tier C through Tier A bands


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
    """Returns {symbol: flags} for anything needing options data."""
    data = json.loads((REPO_ROOT / "watchlist.json").read_text())
    return {
        sym: flags for sym, flags in data["tickers"].items()
        if flags.get("technicals") or flags.get("wheel")
    }


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def put_delta(spot, strike, dte, iv):
    """Black-Scholes put delta (negative). Returns None on degenerate inputs.

    Dividends are not modelled, so deltas on high-yield names run slightly
    large in magnitude — adequate for strike selection, not for hedging.
    """
    if not all([spot, strike, iv]) or dte <= 0 or iv <= 0:
        return None
    t = dte / 365.0
    try:
        d1 = (math.log(spot / strike) + (R_FREE + iv * iv / 2.0) * t) / (iv * math.sqrt(t))
    except (ValueError, ZeroDivisionError):
        return None
    return norm_cdf(d1) - 1.0


def wheel_block(tk, chains, term_structure, spot, today):
    """Cash-secured-put candidates at the wheel's target deltas.

    Picks the expiry inside the framework's 30-45 DTE window (nearest to 37 if
    several qualify, else the closest available, flagged), computes a BS delta
    for every put with usable IV, and returns the strike closest to each target
    delta with the quote detail hard gate 4 needs (OI >= 500, spread <= 8% of mid).
    """
    in_window = [p for p in term_structure if WHEEL_DTE_MIN <= p["dte"] <= WHEEL_DTE_MAX]
    if in_window:
        chosen = min(in_window, key=lambda p: abs(p["dte"] - 37))
        in_target_window = True
    else:
        future = [p for p in term_structure if p["dte"] > 0]
        if not future:
            return {"error": "no future expirations"}
        chosen = min(future, key=lambda p: min(abs(p["dte"] - WHEEL_DTE_MIN),
                                               abs(p["dte"] - WHEEL_DTE_MAX)))
        in_target_window = False

    puts = chains[chosen["expiration"]].puts
    puts = puts[(puts["impliedVolatility"] > 0.01) & (puts["strike"] < spot)]
    if puts.empty:
        return {"error": "no usable OTM puts on the chosen expiry"}

    candidates = []
    for _, row in puts.iterrows():
        d = put_delta(spot, float(row["strike"]), chosen["dte"], float(row["impliedVolatility"]))
        if d is None:
            continue
        bid = float(row["bid"]) if row["bid"] == row["bid"] else 0.0
        ask = float(row["ask"]) if row["ask"] == row["ask"] else 0.0
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else None
        oi = int(row["openInterest"]) if row["openInterest"] == row["openInterest"] else 0
        spread_pct = clean((ask - bid) / mid * 100, 2) if mid else None
        annualised = None
        if mid:
            # (premium per share / strike) * (365 / DTE) — the x100s cancel.
            annualised = clean(mid / float(row["strike"]) * (365.0 / chosen["dte"]) * 100, 2)
        candidates.append({
            "strike": clean(float(row["strike"]), 2),
            "delta": clean(abs(d), 3),
            "bid": clean(bid, 2),
            "ask": clean(ask, 2),
            "mid": clean(mid, 2) if mid else None,
            "open_interest": oi,
            "iv": clean(float(row["impliedVolatility"])),
            "spread_pct_of_mid": spread_pct,
            "collateral_usd": clean(float(row["strike"]) * 100, 2),
            "annualised_yield_pct": annualised,
            "gate4_liquidity_ok": bool(oi >= 500 and spread_pct is not None and spread_pct <= 8),
        })

    if not candidates:
        return {"error": "delta computation failed for all strikes"}

    by_delta = {}
    for target in WHEEL_TARGET_DELTAS:
        best = dict(min(candidates, key=lambda c: abs(c["delta"] - target)))
        best["target_delta"] = target
        best["delta_gap"] = clean(abs(best["delta"] - target), 3)
        # Coarse strike ladders can leave the nearest strike well off target, and
        # two targets can collapse onto one strike. Say so rather than implying
        # a precise delta match.
        best["delta_match_ok"] = best["delta_gap"] <= 0.03
        by_delta[f"{target:.2f}"] = best

    return {
        "expiration": chosen["expiration"],
        "dte": chosen["dte"],
        "in_30_45_window": in_target_window,
        "spot": clean(spot, 2),
        "strikes_by_target_delta": by_delta,
        "note": "annualised_yield_pct is gross of commission; subtract round-trip fees before "
                "comparing to the tier hurdle. Delta is Black-Scholes, no dividend adjustment.",
    }


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


def analyse(symbol, today, flags):
    tk = yf.Ticker(symbol)
    hist = tk.history(period="5d")
    if hist.empty:
        return {"error": "no price data"}
    # Yahoo can return a trailing bar with volume but NaN OHLC. A NaN spot makes
    # every strike comparison false and every "nearest strike" lookup arbitrary,
    # which silently corrupts ATM IV, skew, max pain and the whole wheel block.
    hist = hist[hist["Close"].notna()]
    if hist.empty:
        return {"error": "no priced sessions in window"}
    spot = float(hist["Close"].iloc[-1])
    if not math.isfinite(spot) or spot <= 0:
        return {"error": f"unusable spot price: {spot}"}

    all_expirations = list(tk.options)
    if not all_expirations:
        return {"error": "no options data"}

    expirations = all_expirations[:MAX_EXPIRIES]
    if flags.get("wheel"):
        # Weekly-optionable names can have all 6 nearest expiries inside 30 DTE,
        # so the wheel's 30-45 DTE window would be missed entirely. Extend just
        # far enough to cover it.
        for exp in all_expirations[MAX_EXPIRIES:]:
            if exp in expirations:
                continue
            covered = any(
                WHEEL_DTE_MIN <= (datetime.strptime(e, "%Y-%m-%d").date() - today).days <= WHEEL_DTE_MAX
                for e in expirations
            )
            if covered:
                break
            expirations.append(exp)
            if (datetime.strptime(exp, "%Y-%m-%d").date() - today).days > WHEEL_DTE_MAX:
                break

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
    result = {
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
    if flags.get("wheel"):
        result["wheel"] = wheel_block(tk, chains, term_structure, spot, today)
    return result


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
    for symbol, flags in load_watchlist().items():
        try:
            results[symbol] = analyse(symbol, today, flags)
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
