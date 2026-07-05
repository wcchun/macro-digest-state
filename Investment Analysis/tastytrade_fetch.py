#!/usr/bin/env python3
"""
Tastytrade Data Fetcher — Clean Edition
=========================================
Uses only confirmed-working REST endpoints based on debug output.
Live option Greeks use Black-Scholes calculation from confirmed IV data.
Stock price falls back to Yahoo Finance when Tastytrade REST doesn't provide it.

Usage:
    python tastytrade_fetch.py           # normal mode
    python tastytrade_fetch.py --debug TICKER  # raw API dump

Requirements:
    pip install requests

Secrets required (Codespace secrets):
    TT_REFRESH_TOKEN
    TT_CLIENT_SECRET
"""

import requests
import sys
import os
import math
import json
from datetime import datetime, date, timezone, timedelta

# ── Config ─────────────────────────────────────────────────────────────────────
CLIENT_ID = "1759b6d7-d23c-4e90-9457-a2d4b625af5a"
TOKEN_URL = "https://api.tastytrade.com/oauth/token"
BASE_URL  = "https://api.tastytrade.com"
HEADERS   = {"Content-Type": "application/json"}

# ── Auth ───────────────────────────────────────────────────────────────────────

def get_access_token() -> str:
    refresh_token = os.environ.get("TT_REFRESH_TOKEN", "").strip()
    client_secret = os.environ.get("TT_CLIENT_SECRET", "").strip()

    if not refresh_token:
        print("\n❌  TT_REFRESH_TOKEN not found in environment.")
        print("    Add it as a Codespace secret and restart the Codespace.")
        sys.exit(1)
    if not client_secret:
        print("\n❌  TT_CLIENT_SECRET not found in environment.")
        print("    Add it as a Codespace secret and restart the Codespace.")
        sys.exit(1)

    r = requests.post(TOKEN_URL, data={
        "grant_type":    "refresh_token",
        "client_id":     CLIENT_ID,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    })

    if r.status_code != 200:
        print(f"\n❌  Token refresh failed (HTTP {r.status_code}): {r.text}")
        sys.exit(1)

    access_token = r.json().get("access_token")
    if not access_token:
        print(f"\n❌  No access token in response: {r.text}")
        sys.exit(1)

    print("✅  Authenticated with Tastytrade\n")
    return access_token


def auth_headers(token: str) -> dict:
    return {**HEADERS, "Authorization": f"Bearer {token}"}


# ── Market Metrics ─────────────────────────────────────────────────────────────

def fetch_market_metrics(token: str, ticker: str) -> dict:
    """
    Confirmed working endpoint. Returns IV30, IVR, HV30, beta,
    per-expiry IV term structure, earnings data, and more.
    """
    url = f"{BASE_URL}/market-metrics"
    r   = requests.get(url, params={"symbols": ticker},
                       headers=auth_headers(token))
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}: {r.text}"}
    items = r.json().get("data", {}).get("items", [])
    if not items:
        return {"error": "No market metrics returned"}
    m = items[0]

    # Extract earnings info from nested earnings object
    earnings_obj  = m.get("earnings") or {}
    earnings_date = None
    if isinstance(earnings_obj, dict):
        earnings_date = earnings_obj.get("expected-report-date") or \
                        earnings_obj.get("estimated-report-date") or \
                        earnings_obj.get("event-date")

    # Per-expiry IV term structure
    expiry_ivs = m.get("option-expiration-implied-volatilities", [])

    return {
        "iv30":           m.get("implied-volatility-30-day"),
        "iv_index":       m.get("implied-volatility-index"),       # current spot IV
        "iv_index_15d":   m.get("implied-volatility-index-15-day"),
        "iv_rank":        m.get("implied-volatility-index-rank"),   # confirmed field name
        "iv_percentile":  m.get("implied-volatility-percentile"),
        "hv30":           m.get("historical-volatility-30-day"),
        "hv60":           m.get("historical-volatility-60-day"),
        "iv_hv_diff":     m.get("iv-hv-30-day-difference"),
        "beta":           m.get("beta"),
        "liq_rating":     m.get("liquidity-rating"),
        "liq_rank":       m.get("liquidity-rank"),
        "market_cap":     m.get("market-cap"),
        "sector":         m.get("sector"),
        "industry":       m.get("industry"),
        "earnings_date":  earnings_date,
        "earnings_obj":   earnings_obj,
        "expiry_ivs":     expiry_ivs,   # full term structure
    }


# ── Stock Quote (Yahoo Finance fallback) ───────────────────────────────────────

def fetch_quote_yahoo(ticker: str) -> dict:
    """
    Tastytrade REST does not expose a simple equity quote endpoint —
    live prices require their DXLink websocket streamer.
    Yahoo Finance provides a reliable REST fallback.
    """
    url     = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params  = {"interval": "1d", "range": "5d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code != 200:
            return {"error": f"Yahoo HTTP {r.status_code}"}
        data   = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return {"error": "No data from Yahoo Finance"}
        meta   = result[0].get("meta", {})
        closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
        volumes= result[0].get("indicators", {}).get("quote", [{}])[0].get("volume", [])
        highs  = result[0].get("indicators", {}).get("quote", [{}])[0].get("high", [])
        lows   = result[0].get("indicators", {}).get("quote", [{}])[0].get("low", [])
        # Filter None values
        closes  = [c for c in closes  if c is not None]
        volumes = [v for v in volumes if v is not None]
        highs   = [h for h in highs   if h is not None]
        lows    = [l for l in lows    if l is not None]
        return {
            "last_close":    closes[-1]  if closes  else meta.get("regularMarketPrice"),
            "prior_close":   closes[-2]  if len(closes) > 1 else meta.get("previousClose"),
            "high":          highs[-1]   if highs   else None,
            "low":           lows[-1]    if lows    else None,
            "volume":        volumes[-1] if volumes else None,
            "source":        "Yahoo Finance",
        }
    except Exception as e:
        return {"error": f"Yahoo Finance exception: {e}"}


# ── Option Chain (symbol list only — Greeks via B-S) ──────────────────────────

def fetch_option_chain_nested(token: str, ticker: str) -> dict:
    """
    GET /option-chains/{symbol}/nested
    Returns expirations and strike symbols.
    Live bid/ask/Greeks are NOT available via REST — they require DXLink.
    We use this to get the strike list, then compute Greeks via Black-Scholes.
    """
    url = f"{BASE_URL}/option-chains/{ticker}/nested"
    r   = requests.get(url, headers=auth_headers(token))
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}: {r.text}"}
    data  = r.json().get("data", {})
    items = data.get("items", [])
    if not items:
        return {"error": "No option chain data returned"}
    return {"items": items}


def get_expirations_from_chain(chain_data: dict) -> list:
    """Extract sorted list of expirations from nested chain."""
    if "error" in chain_data:
        return []
    expirations = []
    for underlying in chain_data.get("items", []):
        for exp in underlying.get("expirations", []):
            exp_date = exp.get("expiration-date")
            dte_val  = exp.get("days-to-expiration")
            exp_type = exp.get("expiration-type", "")
            strikes  = exp.get("strikes", [])
            if exp_date and dte_val is not None:
                expirations.append({
                    "expiration-date":    exp_date,
                    "days-to-expiration": dte_val,
                    "expiration-type":    exp_type,
                    "strikes":            strikes,
                })
    return sorted(expirations, key=lambda x: x.get("days-to-expiration", 9999))


def get_atm_strike_from_chain(expirations: list, expiration_date: str,
                               stock_price: float) -> float | None:
    """Find ATM strike for a given expiration from the chain."""
    for exp in expirations:
        if exp.get("expiration-date") == expiration_date:
            strike_prices = []
            for s in exp.get("strikes", []):
                try:
                    strike_prices.append(float(s.get("strike-price", 0)))
                except (ValueError, TypeError):
                    pass
            if strike_prices:
                return min(strike_prices, key=lambda x: abs(x - stock_price))
    return None


# ── Black-Scholes Greeks ───────────────────────────────────────────────────────

def norm_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def bs_greeks(S: float, K: float, T: float, sigma: float,
              r: float = 0.045) -> dict:
    """
    Full Black-Scholes Greeks for a straddle.
    S = stock price, K = strike, T = time in years,
    sigma = annualised IV (decimal), r = risk-free rate.
    Returns per-contract values (×100 shares).
    """
    if T <= 0 or sigma <= 0:
        return {"error": "Invalid T or sigma"}
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)

        call_price = S * norm_cdf(d1)  - K * math.exp(-r * T) * norm_cdf(d2)
        put_price  = K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)
        straddle   = call_price + put_price

        call_delta = norm_cdf(d1)
        put_delta  = norm_cdf(d1) - 1
        net_delta  = 2 * norm_cdf(d1) - 1   # straddle net delta

        gamma_single  = norm_pdf(d1) / (S * sigma * math.sqrt(T))
        gamma_straddle= 2 * gamma_single

        vega_single   = S * norm_pdf(d1) * math.sqrt(T) / 100  # per 1% vol move
        vega_straddle = 2 * vega_single

        theta_call = (-(S * norm_pdf(d1) * sigma) / (2 * math.sqrt(T))
                      - r * K * math.exp(-r * T) * norm_cdf(d2)) / 365
        theta_put  = (-(S * norm_pdf(d1) * sigma) / (2 * math.sqrt(T))
                      + r * K * math.exp(-r * T) * norm_cdf(-d2)) / 365
        theta_straddle = theta_call + theta_put

        return {
            "call_price":      round(call_price,  2),
            "put_price":       round(put_price,   2),
            "straddle_cost":   round(straddle,    2),
            "call_delta":      round(call_delta,  3),
            "put_delta":       round(put_delta,   3),
            "net_delta":       round(net_delta,   4),
            "gamma":           round(gamma_straddle, 5),
            "vega_per_1pct":   round(vega_straddle,  2),
            "theta_per_day":   round(theta_straddle, 2),
            "d1": d1, "d2": d2,
            "upper_be":        round(K + straddle, 2),
            "lower_be":        round(K - straddle, 2),
        }
    except Exception as e:
        return {"error": str(e)}


def get_event_iv(expiry_ivs: list, expiration_date: str) -> float | None:
    """
    Extract the per-expiry IV for a specific expiration from the
    option-expiration-implied-volatilities term structure.
    These are annualised IV values direct from Tastytrade.
    """
    for item in expiry_ivs:
        if item.get("expiration-date") == expiration_date:
            try:
                return float(item.get("implied-volatility", 0))
            except (ValueError, TypeError):
                pass
    return None


# ── IV Crush estimate ──────────────────────────────────────────────────────────

def calc_iv_crush(S: float, K: float, T_remaining: float,
                  event_iv: float, post_iv: float, r: float = 0.045) -> dict:
    """
    Estimates IV crush impact by comparing straddle value at
    event_iv vs post-event iv30 with remaining DTE.
    """
    pre  = bs_greeks(S, K, T_remaining, event_iv, r)
    post = bs_greeks(S, K, T_remaining, post_iv,  r)
    if "error" in pre or "error" in post:
        return {"error": "Could not compute IV crush"}
    crush = round(pre["straddle_cost"] - post["straddle_cost"], 2)
    return {
        "pre_iv":          round(event_iv * 100, 1),
        "post_iv":         round(post_iv  * 100, 1),
        "pre_straddle":    pre["straddle_cost"],
        "post_straddle":   post["straddle_cost"],
        "iv_crush_dollar": crush,
        "iv_crush_pct":    round(crush / pre["straddle_cost"] * 100, 1)
                           if pre["straddle_cost"] else None,
    }


# ── Formatting helpers ─────────────────────────────────────────────────────────

def fmt(val, prefix="", suffix="", decimals=2, fallback="N/A"):
    if val is None:
        return fallback
    try:
        return f"{prefix}{float(val):.{decimals}f}{suffix}"
    except (ValueError, TypeError):
        return str(val)

def pct(v, fb="N/A"):    return fmt(v, suffix="%",  decimals=1, fallback=fb)
def dollar(v, fb="N/A"): return fmt(v, prefix="$", decimals=2, fallback=fb)
def pct_raw(v, fb="N/A"):  # v is already a decimal e.g. 0.279
    if v is None: return fb
    try: return f"{float(v)*100:.1f}%"
    except: return fb


# ── Main output block ──────────────────────────────────────────────────────────

def print_analysis_block(ticker: str, metrics: dict, quote: dict,
                         expirations: list, structures: list):
    now_myt = datetime.now(timezone(timedelta(hours=8)))
    now_edt = datetime.now(timezone(timedelta(hours=-4)))

    print("\n" + "═" * 65)
    print(f"  TASTYTRADE DATA BLOCK — {ticker.upper()}")
    print(f"  Generated : {now_myt.strftime('%Y-%m-%d %H:%M')} MYT  "
          f"({now_edt.strftime('%H:%M')} EDT)")
    print("═" * 65)

    # ── Phase 0: Price ──
    src = quote.get("source", "")
    print(f"\n▌ PHASE 0 — PRICE  (✓ Confirmed via {src})")
    print(f"  Last Close   : {dollar(quote.get('last_close'))}")
    print(f"  Prior Close  : {dollar(quote.get('prior_close'))}")
    print(f"  Today High   : {dollar(quote.get('high'))}")
    print(f"  Today Low    : {dollar(quote.get('low'))}")
    vol = quote.get("volume")
    print(f"  Volume       : {int(vol):,}" if isinstance(vol,(int,float))
          else "  Volume       : N/A")

    # ── Phase 1: IV / IVR ──
    print("\n▌ PHASE 1 — IV / IVR  (✓ Confirmed via Tastytrade API)")
    iv30      = metrics.get("iv30")
    iv_index  = metrics.get("iv_index")
    ivr       = metrics.get("iv_rank")
    hv30      = metrics.get("hv30")
    iv_hv     = metrics.get("iv_hv_diff")

    # API format note (from confirmed debug output):
    # iv_index / iv_percentile  → decimal (e.g. 0.600)  → use pct_raw (×100)
    # iv30 / hv30 / iv_hv / ivr → already % (e.g. 27.85) → use pct (as-is)
    print(f"  IV Index (spot)    : {pct_raw(iv_index)}")
    print(f"  IV30 (30-day avg)  : {pct(iv30)}")
    print(f"  HV30 (historical)  : {pct(hv30)}")
    print(f"  IV-HV30 Diff       : {pct(iv_hv)}")
    print(f"  IV Rank (IVR)      : {pct_raw(ivr)}")               # decimal e.g. 1.011 = 101.1%
    print(f"  IV Percentile      : {pct_raw(metrics.get('iv_percentile'))}")
    print(f"  Earnings Date      : {metrics.get('earnings_date') or 'Not confirmed'}")
    print(f"  Beta               : {fmt(metrics.get('beta'), decimals=2)}")
    print(f"  Sector             : {metrics.get('sector','N/A')}")
    print(f"  Liquidity Rating   : {metrics.get('liq_rating','N/A')} / 3")

    if ivr is not None:
        try:
            ivr_pct = float(ivr) * 100  # decimal e.g. 1.011 → 101.1%
            gate    = ("✅ PASS (IVR < 30)"         if ivr_pct < 30 else
                       "⚠️  BORDERLINE (IVR 30–40)" if ivr_pct < 40 else
                       "❌ FAIL (IVR > 40 — expensive)")
            print(f"\n  IVR Framework Gate : {gate}  ({ivr_pct:.1f}%)")
        except (ValueError, TypeError):
            pass

    # ── IV Term Structure ──
    expiry_ivs = metrics.get("expiry_ivs", [])
    if expiry_ivs:
        print("\n▌ IV TERM STRUCTURE  (✓ Confirmed via Tastytrade API)")
        today = date.today()
        for item in expiry_ivs[:10]:
            exp_d  = item.get("expiration-date", "")
            iv_val = item.get("implied-volatility")
            try:
                exp_date_obj = date.fromisoformat(exp_d)
                dte_val      = (exp_date_obj - today).days
                iv_pct       = float(iv_val) * 100 if iv_val else None
                if dte_val < 0:
                    continue  # skip expired expirations — IV values are stale artefacts
                marker = " ◄ earnings expiry" if dte_val <= 7 else ""
                print(f"  {exp_d}  ({dte_val:3d} DTE)  "
                      f"IV: {iv_pct:.1f}%{marker}" if iv_pct else
                      f"  {exp_d}  ({dte_val:3d} DTE)  IV: N/A")
            except Exception:
                pass

    # ── Phase 2: Option Structures ──
    print("\n▌ PHASE 2 — STRADDLE STRUCTURES  ([Calculated] via Black-Scholes)")
    print("  Note: Greeks calculated from confirmed Tastytrade IV data.")
    print("  Bid/ask spreads not available via REST — verify on live platform.\n")

    stock_p = quote.get("last_close")
    iv30_f  = float(iv30) / 100 if iv30 else None  # API returns %, convert to decimal for B-S

    for struct in structures:
        exp_date  = struct["expiration_date"]
        dte       = struct["dte"]
        atm       = struct["atm_strike"]
        event_iv  = struct["event_iv"]
        greeks    = struct["greeks"]
        crush     = struct["iv_crush"]

        # Event IV source label
        if event_iv and iv30_f:
            iv_source = ("✓ From Tastytrade term structure"
                         if struct.get("iv_from_term_structure")
                         else "[Estimated: IV30 × 1.55]")
        else:
            iv_source = "N/A"

        print(f"  ┌─ {struct['label']}")
        print(f"  │  Expiry: {exp_date}  |  DTE: {dte}  "
              f"|  ATM Strike: {dollar(atm)}")
        print(f"  │  Event IV used    : "
              f"{round(event_iv*100,1)}%  ({iv_source})"
              if event_iv else "  │  Event IV used    : N/A")

        if "error" not in greeks:
            sc = greeks["straddle_cost"]
            print(f"  │  Straddle Cost    : {dollar(sc)} / share  "
                  f"({dollar(sc*100)} / contract)")
            print(f"  │  Upper Break-Even : {dollar(greeks['upper_be'])}")
            print(f"  │  Lower Break-Even : {dollar(greeks['lower_be'])}")

            # Implied move validity check
            if stock_p and iv30_f:
                impl_move = float(stock_p) * iv30_f * math.sqrt(dte/365)
                gate2 = "✅ PASS" if sc < impl_move else "❌ FAIL"
                print(f"  │  Implied Move ($) : {dollar(impl_move)}  "
                      f"→  Straddle < Implied Move? {gate2}")

            print(f"  │")
            print(f"  │  Greeks (per straddle, 1 contract = 100 shares):")
            print(f"  │    Net Delta  : {greeks['net_delta']:+.4f}  "
                  f"(Call Δ: {greeks['call_delta']:.3f}  "
                  f"Put Δ: {greeks['put_delta']:.3f})")
            print(f"  │    Gamma      : {greeks['gamma']:.5f} / $1 move")
            print(f"  │    Vega       : {dollar(greeks['vega_per_1pct'])} "
                  f"per 1% IV change")
            print(f"  │    Theta      : {dollar(greeks['theta_per_day'])} "
                  f"per day  "
                  f"({abs(greeks['theta_per_day']/sc*100):.1f}% of premium/day)"
                  if sc else "")
        else:
            print(f"  │  Greeks error: {greeks['error']}")

        # IV Crush
        if crush and "error" not in crush:
            print(f"  │")
            print(f"  │  IV Crush Estimate ([Calculated]):")
            print(f"  │    Pre-event IV    : {crush['pre_iv']}%  →  "
                  f"Post-event IV: {crush['post_iv']}%")
            print(f"  │    Straddle before : {dollar(crush['pre_straddle'])}  →  "
                  f"After: {dollar(crush['post_straddle'])}")
            print(f"  └─  IV Crush impact : {dollar(crush['iv_crush_dollar'])} / share  "
                  f"({crush['iv_crush_pct']}% of premium)")
        else:
            print(f"  └─")
        print()

    # ── Expirations ──
    print("▌ AVAILABLE EXPIRATIONS (nearest 8)")
    for e in expirations[:8]:
        print(f"  {e.get('expiration-date','N/A')}  "
              f"({e.get('days-to-expiration','N/A'):3} DTE)  "
              f"[{e.get('expiration-type','')}]")

    print("\n" + "═" * 65)
    print("  END OF DATA BLOCK — paste everything above into Claude")
    print("═" * 65 + "\n")


# ── Debug mode ─────────────────────────────────────────────────────────────────

def debug_endpoints(token: str, ticker: str):
    endpoints = [
        ("Market Metrics",       f"{BASE_URL}/market-metrics",
         {"symbols": ticker}),
        ("Option Chain Nested",  f"{BASE_URL}/option-chains/{ticker}/nested",
         {}),
        ("Option Chain Compact", f"{BASE_URL}/option-chains/{ticker}/compact",
         {}),
    ]
    for name, url, params in endpoints:
        print(f"\n{'─'*65}\n  ENDPOINT: {name}\n  URL: {url}\n{'─'*65}")
        r = requests.get(url, params=params, headers=auth_headers(token))
        print(f"  HTTP Status: {r.status_code}")
        try:
            parsed = r.json()
            print(json.dumps(parsed, indent=2)[:2000])
        except Exception:
            print(r.text[:1000])


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    print("═" * 65)
    print("  TASTYTRADE DATA FETCHER")
    print("═" * 65 + "\n")

    access_token = get_access_token()

    ticker_input = input("Ticker(s) to analyse (e.g. AAPL or AAPL,NVDA): ").strip()
    tickers      = [t.strip().upper() for t in ticker_input.split(",") if t.strip()]
    if not tickers:
        print("No tickers provided. Exiting.")
        sys.exit(1)

    n_input = input("Number of expiry structures to analyse [2]: ").strip()
    n_exp   = int(n_input) if n_input.isdigit() else 2

    # Risk-free rate (approximate — update periodically)
    r_free = 0.045

    for ticker in tickers:
        print(f"\n{'─'*65}\n  Fetching {ticker}...\n{'─'*65}")

        # Fetch confirmed data
        metrics = fetch_market_metrics(access_token, ticker)
        if "error" in metrics:
            print(f"  ❌  Metrics error: {metrics['error']}")
            continue

        quote = fetch_quote_yahoo(ticker)
        if "error" in quote:
            print(f"  ⚠️  Quote error: {quote['error']}")

        # Option chain — for strikes
        print("  → Fetching option chain...")
        chain_data   = fetch_option_chain_nested(access_token, ticker)
        expirations  = get_expirations_from_chain(chain_data)

        if not expirations:
            print("  ⚠️  No expirations from chain — using term structure dates only")
            # Fall back to term structure dates
            expiry_ivs  = metrics.get("expiry_ivs", [])
            today       = date.today()
            expirations = []
            for item in expiry_ivs:
                exp_d = item.get("expiration-date", "")
                try:
                    dte_val = (date.fromisoformat(exp_d) - today).days
                    if dte_val > 0:
                        expirations.append({
                            "expiration-date":    exp_d,
                            "days-to-expiration": dte_val,
                            "expiration-type":    "Standard",
                            "strikes":            [],
                        })
                except Exception:
                    pass

        stock_price = None
        try:
            stock_price = float(quote.get("last_close") or 0)
        except (ValueError, TypeError):
            pass

        iv30_f     = float(metrics["iv30"]) / 100 if metrics.get("iv30") else None  # API returns %, B-S needs decimal
        expiry_ivs = metrics.get("expiry_ivs", [])

        # Build structures for nearest N valid expirations
        future_exps = [e for e in expirations
                       if e.get("days-to-expiration", 0) > 0][:n_exp]

        structures = []
        for i, exp in enumerate(future_exps):
            exp_date = exp["expiration-date"]
            dte      = exp["days-to-expiration"]
            T        = dte / 365

            # ATM strike
            atm = None
            if stock_price and exp.get("strikes"):
                atm = get_atm_strike_from_chain(expirations, exp_date, stock_price)
            if not atm and stock_price:
                # Round to nearest $2.50 as fallback
                atm = round(stock_price / 2.5) * 2.5

            # Event IV: use per-expiry term structure if available, else estimate
            event_iv          = get_event_iv(expiry_ivs, exp_date)
            iv_from_ts        = event_iv is not None
            if not event_iv and iv30_f:
                # Short-dated: apply earnings uplift; longer: use IV30
                event_iv = iv30_f * 1.55 if dte <= 7 else iv30_f * 1.05

            # Greeks
            greeks = {}
            if stock_price and atm and event_iv:
                greeks = bs_greeks(stock_price, atm, T, event_iv, r_free)

            # IV Crush
            crush = {}
            if stock_price and atm and event_iv and iv30_f and dte <= 10:
                # Post-earnings: assume IV reverts to IV30
                crush = calc_iv_crush(stock_price, atm, T,
                                      event_iv, iv30_f, r_free)

            label = (f"Structure A — Event Capture ({dte} DTE)"
                     if i == 0 else
                     f"Structure B — Trend Capture ({dte} DTE)")

            structures.append({
                "label":               label,
                "expiration_date":     exp_date,
                "dte":                 dte,
                "atm_strike":          atm,
                "event_iv":            event_iv,
                "iv_from_term_structure": iv_from_ts,
                "greeks":              greeks,
                "iv_crush":            crush,
            })

        print_analysis_block(ticker, metrics, quote, expirations, structures)

    print("Done. Paste the data block(s) above into Claude.\n")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--debug":
        print("═" * 65)
        print("  TASTYTRADE DEBUG MODE")
        print("═" * 65 + "\n")
        token = get_access_token()
        debug_endpoints(token, sys.argv[2].upper())
    else:
        main()
