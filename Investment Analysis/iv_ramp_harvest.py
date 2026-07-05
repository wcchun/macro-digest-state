#!/usr/bin/env python3
"""
IV Ramp Harvest — Pre-Earnings Same-Day Vega Scalp
===================================================
Fetches and evaluates all data for the Pre-Earnings IV Ramp Harvest strategy.
Run on the day of an AMC (After-Market-Close) earnings announcement.

MODES (auto-detected from EDT time, or override with a flag):
  python iv_ramp_harvest.py AAPL              # auto — runs the right phase for the time
  python iv_ramp_harvest.py AAPL --presession # Phase 0+1: baseline before the open
  python iv_ramp_harvest.py AAPL --open       # 9:30–10:00 AM EDT: capture IV_open
  python iv_ramp_harvest.py AAPL --check      # 11:45 AM–1:30 PM EDT: Phase 2 gate check
  python iv_ramp_harvest.py AAPL --exit       # 3:00–3:45 PM EDT: Phase 4 exit monitor
  python iv_ramp_harvest.py AAPL --reset      # Clear saved state (new ticker / new day)

MYT NOTE: EDT = UTC−4 | MYT = UTC+8 | MYT = EDT + 12h
  Open capture  : 9:30–10:00 PM MYT (same day)
  Entry window  : 11:45 PM–1:30 AM MYT
  Exit window   : 3:00–3:45 AM MYT (next day)
  HARD STOP     : 3:45 AM MYT — flat before 4:00 AM print

Requirements:
  pip install requests

Secrets (Codespace secrets):
  TT_REFRESH_TOKEN
  TT_CLIENT_SECRET
"""

import argparse
import json
import math
import os
import sys
import requests
from datetime import datetime, date, timezone, timedelta

# ── Constants ──────────────────────────────────────────────────────────────────
CLIENT_ID = "1759b6d7-d23c-4e90-9457-a2d4b625af5a"
TOKEN_URL = "https://api.tastytrade.com/oauth/token"
BASE_URL  = "https://api.tastytrade.com"
HEADERS   = {"Content-Type": "application/json"}
R_FREE    = 0.045       # risk-free rate — update periodically
EDT       = timezone(timedelta(hours=-4))
MYT       = timezone(timedelta(hours=8))

# ── U-Curve: 30-min bucket → % of daily volume (from volume_analyst_system_prompt.md)
# Each key is (EDT_hour, EDT_minute_start_of_bucket).
# Values represent the fraction of ADV expected in that 30-min window.
UCURVE: dict[tuple[int, int], float] = {
    (9,  30): 0.140,
    (10,  0): 0.085,
    (10, 30): 0.065,
    (11,  0): 0.055,
    (11, 30): 0.045,
    (12,  0): 0.040,
    (12, 30): 0.040,
    (13,  0): 0.045,
    (13, 30): 0.055,
    (14,  0): 0.065,
    (14, 30): 0.075,
    (15,  0): 0.095,
    (15, 30): 0.150,
}
UCURVE_NAIVE = 0.077   # 100% / 13 buckets — baseline if time lookup fails


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════════════════════

def get_access_token() -> str:
    rt = os.environ.get("TT_REFRESH_TOKEN", "").strip()
    cs = os.environ.get("TT_CLIENT_SECRET", "").strip()
    if not rt:
        sys.exit("❌  TT_REFRESH_TOKEN not set. Add as a Codespace secret.")
    if not cs:
        sys.exit("❌  TT_CLIENT_SECRET not set. Add as a Codespace secret.")
    r = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token", "client_id": CLIENT_ID,
        "client_secret": cs, "refresh_token": rt,
    })
    if r.status_code != 200:
        sys.exit(f"❌  Token refresh failed (HTTP {r.status_code}): {r.text}")
    token = r.json().get("access_token")
    if not token:
        sys.exit(f"❌  No access_token in response: {r.text}")
    print("✅  Authenticated with Tastytrade\n")
    return token

def auth_headers(token: str) -> dict:
    return {**HEADERS, "Authorization": f"Bearer {token}"}


# ═══════════════════════════════════════════════════════════════════════════════
#  TASTYTRADE DATA
# ═══════════════════════════════════════════════════════════════════════════════

def _f(val) -> float | None:
    """Safe string-to-float conversion for Tastytrade API fields."""
    try:
        return float(val) if val is not None else None
    except (ValueError, TypeError):
        return None

def fetch_market_metrics(token: str, ticker: str) -> dict:
    """
    Returns IV30, IVR, HV30, per-expiry term structure, earnings date.
    API format notes (from tastytrade_fetch.py debug output):
      iv_index / iv_percentile / iv_rank → decimal (e.g. 0.65) → multiply x100 for %
      iv30 / hv30                        → already % (e.g. 48.2)
    All numeric fields coerced to float via _f() — API may return strings or None.
    """
    r = requests.get(f"{BASE_URL}/market-metrics",
                     params={"symbols": ticker},
                     headers=auth_headers(token))
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
    items = r.json().get("data", {}).get("items", [])
    if not items:
        return {"error": "No items in market-metrics response"}
    m    = items[0]
    earn = m.get("earnings") or {}
    return {
        "iv_index":      _f(m.get("implied-volatility-index")),        # decimal
        "iv30":          _f(m.get("implied-volatility-30-day")),        # %
        "iv_rank":       _f(m.get("implied-volatility-index-rank")),    # decimal
        "iv_percentile": _f(m.get("implied-volatility-percentile")),    # decimal
        "hv30":          _f(m.get("historical-volatility-30-day")),     # %
        "iv_hv_diff":    _f(m.get("iv-hv-30-day-difference")),
        "beta":          _f(m.get("beta")),
        "liq_rating":    m.get("liquidity-rating"),
        "sector":        m.get("sector"),
        "earnings_date": (earn.get("expected-report-date") or
                          earn.get("estimated-report-date") or
                          earn.get("event-date")),
        "expiry_ivs":    m.get("option-expiration-implied-volatilities", []),
    }

def fetch_option_expirations(token: str, ticker: str) -> list[dict]:
    """Returns sorted list of future expirations with DTE and strike list."""
    r = requests.get(f"{BASE_URL}/option-chains/{ticker}/nested",
                     headers=auth_headers(token))
    if r.status_code != 200:
        return []
    today = date.today()
    expirations: list[dict] = []
    for underlying in r.json().get("data", {}).get("items", []):
        for exp in underlying.get("expirations", []):
            exp_date = exp.get("expiration-date")
            try:
                dte = (date.fromisoformat(exp_date) - today).days
            except Exception:
                continue
            if dte <= 0:
                continue
            strikes = []
            for s in exp.get("strikes", []):
                try:
                    strikes.append(float(s.get("strike-price", 0)))
                except (ValueError, TypeError):
                    pass
            expirations.append({
                "date":    exp_date,
                "dte":     dte,
                "type":    exp.get("expiration-type", ""),
                "strikes": strikes,
            })
    return sorted(expirations, key=lambda x: x["dte"])

def expiry_iv_pct(expiry_ivs: list, exp_date: str) -> float | None:
    """Per-expiry IV for a specific expiration, returned as % (×100 from decimal)."""
    for item in expiry_ivs:
        if item.get("expiration-date") == exp_date:
            try:
                return float(item["implied-volatility"]) * 100
            except (KeyError, ValueError, TypeError):
                pass
    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  YAHOO FINANCE DATA
# ═══════════════════════════════════════════════════════════════════════════════

_YF_HEADERS = {"User-Agent": "Mozilla/5.0"}

def _yf_chart(ticker: str, interval: str, range_: str) -> dict:
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": interval, "range": range_},
            headers=_YF_HEADERS, timeout=12,
        )
        return r.json()["chart"]["result"][0]
    except Exception as e:
        return {"_error": str(e)}

def fetch_daily_data(ticker: str) -> dict:
    """
    2-month daily OHLCV → 20-day ADV, 20-day ATR, yesterday's pivot S/R levels.
    S/R uses standard pivot formula: Pivot=(H+L+C)/3, R1=2P−L, S1=2P−H etc.
    """
    result = _yf_chart(ticker, "1d", "2mo")
    if "_error" in result:
        return {"error": result["_error"]}
    try:
        q = result["indicators"]["quote"][0]
        def clean(arr): return [v for v in (arr or []) if v is not None]
        closes  = clean(q.get("close",  []))
        highs   = clean(q.get("high",   []))
        lows    = clean(q.get("low",    []))
        volumes = clean(q.get("volume", []))

        n = min(len(closes), len(highs), len(lows), len(volumes))
        closes, highs, lows, volumes = closes[:n], highs[:n], lows[:n], volumes[:n]

        # 20-day ADV
        adv = (sum(volumes[-20:]) / 20) if len(volumes) >= 20 else (
               sum(volumes) / len(volumes) if volumes else None)

        # ATR(20) — True Range for each bar, then 20-day average
        trs: list[float] = []
        for i in range(1, n):
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i-1]),
                     abs(lows[i]  - closes[i-1]))
            trs.append(tr)
        atr = (sum(trs[-20:]) / 20) if len(trs) >= 20 else (
               sum(trs) / len(trs) if trs else None)

        # Pivot S/R from the most-recently-completed session (index -1 or -2)
        # Use the second-to-last row (yesterday) because today's bar may be partial
        ph = highs[-2]  if len(highs)  >= 2 else highs[-1]
        pl = lows[-2]   if len(lows)   >= 2 else lows[-1]
        pc = closes[-2] if len(closes) >= 2 else closes[-1]
        pivot = (ph + pl + pc) / 3
        daily_range = ph - pl

        return {
            "adv_20":      round(adv)             if adv  else None,
            "atr_20":      round(atr,  3)          if atr  else None,
            "atr_20_pct":  round(atr / pc * 100, 2) if (atr and pc) else None,
            "pivot":       round(pivot,       2),
            "r1":          round(2*pivot - pl,          2),
            "r2":          round(pivot + daily_range,   2),
            "s1":          round(2*pivot - ph,          2),
            "s2":          round(pivot - daily_range,   2),
            "prev_high":   round(ph, 2),
            "prev_low":    round(pl, 2),
            "prev_close":  round(pc, 2),
        }
    except Exception as e:
        return {"error": str(e)}

def fetch_quote(ticker: str) -> dict:
    """Current price and prior close via Yahoo Finance."""
    result = _yf_chart(ticker, "1d", "5d")
    if "_error" in result:
        return {"error": result["_error"]}
    try:
        closes = [c for c in result["indicators"]["quote"][0].get("close", []) if c]
        return {
            "price":       closes[-1] if closes            else None,
            "prior_close": closes[-2] if len(closes) >= 2 else None,
        }
    except Exception as e:
        return {"error": str(e)}

def fetch_intraday_candles(ticker: str, interval: str = "15m") -> list[dict]:
    """Today's intraday OHLCV candles as EDT-timestamped dicts."""
    result = _yf_chart(ticker, interval, "1d")
    if "_error" in result:
        return []
    try:
        tss = result.get("timestamp", [])
        q   = result["indicators"]["quote"][0]
        candles: list[dict] = []
        for i, ts in enumerate(tss):
            try:
                o = q["open"][i]; h = q["high"][i]
                l = q["low"][i];  c = q["close"][i]
                v = q["volume"][i]
                if None in (o, h, l, c, v):
                    continue
                candles.append({
                    "time_edt": datetime.fromtimestamp(ts, tz=EDT),
                    "open": o, "high": h, "low": l, "close": c,
                    "volume": v,
                    "candle_range": h - l,
                })
            except Exception:
                continue
        return candles
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════════════
#  BLACK-SCHOLES
# ═══════════════════════════════════════════════════════════════════════════════

def _norm_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def bs_straddle(S: float, K: float, T: float, sigma: float,
                r: float = R_FREE) -> dict:
    """
    ATM straddle price and key Greeks.
    sigma = annualised IV as decimal (e.g. 0.65 for 65%).
    Returns $/share values; multiply by 100 for $/contract.
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {"error": f"Invalid inputs (S={S}, K={K}, T={T}, σ={sigma})"}
    try:
        d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        call = S*_norm_cdf(d1) - K*math.exp(-r*T)*_norm_cdf(d2)
        put  = K*math.exp(-r*T)*_norm_cdf(-d2) - S*_norm_cdf(-d1)
        straddle      = call + put
        vega_straddle = 2 * S * _norm_pdf(d1) * math.sqrt(T) / 100   # per 1% vol pt
        theta_c = (-(S*_norm_pdf(d1)*sigma)/(2*math.sqrt(T))
                   - r*K*math.exp(-r*T)*_norm_cdf(d2)) / 365
        theta_p = (-(S*_norm_pdf(d1)*sigma)/(2*math.sqrt(T))
                   + r*K*math.exp(-r*T)*_norm_cdf(-d2)) / 365
        return {
            "straddle":  round(straddle, 2),
            "call":      round(call,     2),
            "put":       round(put,      2),
            "vega":      round(vega_straddle, 3),         # $/1 vol pt
            "theta_day": round(theta_c + theta_p, 3),    # $/day (negative)
            "upper_be":  round(K + straddle, 2),
            "lower_be":  round(K - straddle, 2),
        }
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
#  U-CURVE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def ucurve_bucket_pct(edt_dt: datetime) -> tuple[float, str]:
    """
    Return (fraction_of_ADV, bucket_label) for the 30-min bucket containing edt_dt.
    For a 15-min candle, multiply by 0.5 to get the expected fraction.
    """
    h, m = edt_dt.hour, edt_dt.minute
    bucket_m = 0 if m < 30 else 30
    pct = UCURVE.get((h, bucket_m), UCURVE_NAIVE)
    end_m = bucket_m + 30
    end_h = h + (1 if end_m >= 60 else 0)
    end_m = end_m % 60
    label = f"{h:02d}:{bucket_m:02d}–{end_h:02d}:{end_m:02d} EDT"
    return pct, label

def expected_15m_vol(adv: float, edt_dt: datetime) -> float:
    """Expected 15-min candle volume from ADV and U-curve."""
    pct, _ = ucurve_bucket_pct(edt_dt)
    return adv * pct * 0.5   # 15 min = half of 30-min bucket


# ═══════════════════════════════════════════════════════════════════════════════
#  STATE FILE  (saved as iv_ramp_state_TICKER.json in current directory)
# ═══════════════════════════════════════════════════════════════════════════════

def _state_path(ticker: str) -> str:
    return f"iv_ramp_state_{ticker.upper()}.json"

def load_state(ticker: str) -> dict:
    p = _state_path(ticker)
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return {"ticker": ticker.upper(), "iv_readings": []}

def save_state(ticker: str, state: dict):
    with open(_state_path(ticker), "w") as f:
        json.dump(state, f, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════════════
#  TIMING HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def now_edt() -> datetime:
    return datetime.now(EDT)

def edt_myt(edt_dt: datetime) -> str:
    """Format as 'HH:MM EDT / HH:MM MYT'."""
    myt_dt = edt_dt.astimezone(MYT)
    return f"{edt_dt.strftime('%H:%M')} EDT / {myt_dt.strftime('%H:%M')} MYT"

def mins_until(edt_target_hm: tuple[int, int]) -> int:
    now = now_edt()
    target = now.replace(hour=edt_target_hm[0], minute=edt_target_hm[1],
                         second=0, microsecond=0)
    return max(0, int((target - now).total_seconds() / 60))

def get_phase(edt_dt: datetime) -> str:
    tm = edt_dt.hour * 60 + edt_dt.minute
    if   tm <  9*60+30:  return "pre-market"
    elif tm < 10*60:     return "open-window"    # 9:30–10:00: capture IV_open
    elif tm < 11*60+45:  return "morning-fade"   # observe
    elif tm < 13*60+30:  return "entry-window"   # 11:45–13:30: ENTRY
    elif tm < 15*60:     return "afternoon"      # hold / monitor
    elif tm < 15*60+45:  return "exit-window"    # 15:00–15:45: EXIT
    elif tm < 16*60:     return "hard-stop"      # must be flat
    else:                return "post-close"

PHASE_LABELS = {
    "pre-market":   "PRE-MARKET — US session not open yet",
    "open-window":  "⚡ OPEN WINDOW (9:30–10:00 EDT / 9:30–10:00 PM MYT) — capture IV_open",
    "morning-fade": "📉 MORNING FADE (10:00–11:45 EDT) — observe IV descent, no entry yet",
    "entry-window": "🎯 ENTRY WINDOW (11:45 EDT–1:30 EDT / 11:45 PM–1:30 AM MYT) — run --check",
    "afternoon":    "📈 AFTERNOON RAMP — position live; monitor IV vs IV_open",
    "exit-window":  "💰 EXIT WINDOW (3:00–3:45 EDT / 3:00–3:45 AM MYT) — run --exit",
    "hard-stop":    "🛑 HARD STOP ZONE — flat by 3:45 EDT (3:45 AM MYT), do NOT hold into print",
    "post-close":   "📋 POST-CLOSE — US session ended",
}

def print_header(title: str):
    now = now_edt()
    print(f"\n{'═'*65}")
    print(f"  {title}")
    print(f"  {edt_myt(now)}  |  {now.astimezone(MYT).strftime('%Y-%m-%d')}")
    print(f"{'═'*65}\n")


# ═══════════════════════════════════════════════════════════════════════════════
#  EXPIRY SELECTION
# ═══════════════════════════════════════════════════════════════════════════════

def select_target_expiry(expirations: list[dict], earnings_date: str | None) -> dict | None:
    """
    Nearest expiry with 7 ≤ DTE ≤ 14 that falls on or after earnings_date.
    Falls back to nearest expiry with 5 ≤ DTE ≤ 21 if nothing in the ideal range.
    Returns None if no suitable expiry found.
    """
    candidates = []
    for exp in expirations:
        after_earnings = (not earnings_date or exp["date"] >= earnings_date)
        if after_earnings:
            candidates.append(exp)

    # Ideal: 7–14 DTE
    ideal = [e for e in candidates if 7 <= e["dte"] <= 14]
    if ideal:
        return ideal[0]

    # Fallback: 5–21 DTE
    fallback = [e for e in candidates if 5 <= e["dte"] <= 21]
    if fallback:
        return fallback[0]

    return None

def nearest_atm_strike(strikes: list[float], price: float) -> float | None:
    if not strikes or not price:
        return None
    return min(strikes, key=lambda x: abs(x - price))


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE: PRE-SESSION (baseline check before the open)
# ═══════════════════════════════════════════════════════════════════════════════

def run_presession(ticker: str, token: str, state: dict):
    print_header(f"IV RAMP HARVEST — {ticker} — PRE-SESSION CHECK")

    print("  → Tastytrade: market metrics + option chain...")
    metrics = fetch_market_metrics(token, ticker)
    if "error" in metrics:
        print(f"  ❌  {metrics['error']}"); return

    expirations = fetch_option_expirations(token, ticker)

    print("  → Yahoo Finance: quote + daily data (ADV / ATR / S/R)...")
    quote = fetch_quote(ticker)
    daily = fetch_daily_data(ticker)
    price = quote.get("price")

    # ── Earnings ──
    earnings_date = metrics.get("earnings_date")
    print(f"  ▌ EARNINGS VERIFICATION")
    print(f"    Tastytrade earnings date : {earnings_date or 'Not available'}")
    print(f"    ⚠️  Cross-check manually  : tipranks.com / marketbeat.com / IR page")
    print(f"    AMC confirmed?           : [USER VERIFY — strategy requires After-Market-Close]")

    # ── Expiry selection ──
    target    = select_target_expiry(expirations, earnings_date)
    front_exp = expirations[0] if expirations else None   # closest expiry regardless of DTE
    ts_ratio  = None   # defined here to avoid NameError if target is None
    iv30      = metrics.get("iv30")

    def _expiry_row(exp, label, price, iv30, metrics):
        """Print one expiry row and return (iv_pct, greeks_dict)."""
        if exp is None:
            return None, {}
        atm_s  = nearest_atm_strike(exp["strikes"], price) if price else None
        iv_pct = expiry_iv_pct(metrics["expiry_ivs"], exp["date"])
        use_iv = iv_pct or (metrics["iv_index"] * 100 if metrics.get("iv_index") else None) or iv30
        g      = bs_straddle(price, atm_s, exp["dte"]/365, use_iv/100) if (
                     price and atm_s and use_iv) else {}
        ts     = (iv_pct / iv30) if (iv_pct and iv30) else None
        ts_sym = ("✅" if ts and ts >= 1.10 else "⚠️ " if ts and ts >= 0.95 else "❌ ") if ts else "N/A"
        sc     = g.get("straddle")
        print(f"    {'─'*56}")
        print(f"    {label}")
        print(f"    Expiry   : {exp['date']}  ({exp['dte']} DTE)  [{exp['type']}]")
        print(f"    ATM      : {'${:.2f}'.format(atm_s) if atm_s else 'N/A'}")
        print(f"    IV       : {f'{iv_pct:.1f}%' if iv_pct else 'N/A'}")
        print(f"    TS Ratio : {f'{ts:.2f}×' if ts else 'N/A'}  {ts_sym}  (vs IV30 {f'{iv30:.1f}%' if iv30 else 'N/A'})")
        if sc:
            print(f"    Cost     : ${sc:.2f}/share  (${sc*100:.0f}/contract)")
            print(f"    Vega     : ${g['vega']:.3f}/vol pt  |  Theta: ${g['theta_day']:.3f}/day "
                  f"({abs(g['theta_day'])/sc*100:.1f}% of premium)")
        return iv_pct, g

    same_expiry = (front_exp and target and front_exp["date"] == target["date"])

    print(f"\n  ▌ EXPIRY ANALYSIS")
    print(f"    Front expiry  = closest listed expiry (max event IV, high theta)")
    print(f"    Target expiry = 7–14 DTE post-earnings (trading vehicle)")
    if same_expiry:
        print(f"    ℹ️  Front = Target — only one expiry in the 7–14 DTE range.")

    front_iv_pre, front_g = _expiry_row(
        front_exp, "FRONT EXPIRY  (closest — max event premium, analysis only)", price, iv30, metrics)

    if not same_expiry:
        near_iv, target_g = _expiry_row(
            target, "TARGET EXPIRY  (trading vehicle — 7–14 DTE post-earnings)", price, iv30, metrics)
        atm = nearest_atm_strike(target["strikes"], price) if (target and price) else None
    else:
        near_iv  = front_iv_pre
        target_g = front_g
        atm      = nearest_atm_strike(front_exp["strikes"], price) if (front_exp and price) else None

    use_iv_pct = near_iv

    # Three-point term structure summary
    front_iv  = front_iv_pre
    target_iv = near_iv
    print(f"\n    {'─'*56}")
    print(f"    TERM STRUCTURE SNAPSHOT  [Calculated]")
    if front_iv and target_iv and iv30:
        print(f"    Front {front_exp['dte']:>2}d : {front_iv:.1f}%  "
              f"({front_iv/iv30:.2f}× IV30)")
        if not same_expiry:
            print(f"    Target {target['dte']:>2}d: {target_iv:.1f}%  "
                  f"({target_iv/iv30:.2f}× IV30)")
        print(f"    IV30      : {iv30:.1f}%  (baseline)")
        if not same_expiry and front_iv and target_iv:
            ft_ratio = front_iv / target_iv
            cal_flag = "  ← calendar viable" if ft_ratio >= 1.20 else ""
            print(f"    Front / Target ratio : {ft_ratio:.2f}×{cal_flag}")
    elif iv30:
        print(f"    IV30 : {iv30:.1f}%  (per-expiry IVs not available)")

    # Gate 5 pre-read
    ts_ratio = (target_iv / iv30) if (target_iv and iv30) else None
    if not target:
        dte_list = [f"{e['date']} ({e['dte']}d)" for e in expirations[:5]]
        print(f"\n    ❌  No suitable post-earnings expiry (7–21 DTE) found.")
        print(f"    Available: {', '.join(dte_list)}")
        print(f"    Consider calendar spread (see system prompt Section 9).")
        atm = None; near_iv = None; use_iv_pct = None

    # ── IV / IVR ──
    iv30  = metrics.get("iv30")
    ivr_p = float(metrics["iv_rank"]) * 100 if metrics.get("iv_rank") else None
    iv_i  = float(metrics["iv_index"]) * 100 if metrics.get("iv_index") else None

    print(f"\n  ▌ IV BASELINE  (✓ Confirmed via Tastytrade)")
    print(f"    IV Index (spot) : {f'{iv_i:.1f}%' if iv_i else 'N/A'}")
    print(f"    IV30            : {f'{iv30:.1f}%' if iv30 else 'N/A'}")
    print(f"    IVR             : {f'{ivr_p:.1f}%' if ivr_p else 'N/A'}")
    hv30_val = metrics.get('hv30')
    print(f"    HV30            : {f'{hv30_val:.1f}%' if hv30_val else 'N/A'}")
    print(f"    Liq rating      : {metrics.get('liq_rating', 'N/A')}/3  "
          f"| Sector: {metrics.get('sector', 'N/A')}")

    # ── Volume / ATR / S/R ──
    if "error" not in daily:
        adv = daily.get("adv_20")
        atr = daily.get("atr_20")
        print(f"\n  ▌ VOLUME & VOLATILITY BASELINE  (✓ Confirmed via Yahoo Finance)")
        print(f"    20-day ADV  : {f'{int(adv):,} shares' if adv else 'N/A'}")
        print(f"    20-day ATR  : {'${:.3f}'.format(atr) if atr else 'N/A'}"
              + (f"  ({daily['atr_20_pct']:.2f}% of price)" if daily.get("atr_20_pct") else ""))
        print(f"\n  ▌ PIVOT S/R LEVELS  (from prior session)")
        for label, key in [("R2", "r2"), ("R1", "r1"), ("Pivot", "pivot"),
                           ("S1", "s1"), ("S2", "s2")]:
            val = daily.get(key)
            if val:
                near = " ◄ nearest to price" if price and abs(val - price) == min(
                    abs(daily.get(k, 9999) - price) for k in ["r2","r1","pivot","s1","s2"]
                ) else ""
                print(f"    {label:<6} : ${val:.2f}{near}")

        # Midday U-curve expected volumes
        if adv:
            print(f"\n  ▌ MIDDAY U-CURVE EXPECTED VOLUME — 15-MIN CANDLE  (ADV = {int(adv):,})")
            print(f"    {'EDT':^20}  {'MYT (next day)':^22}  {'Expected':>10}  {'VDU <0.6×':>12}")
            midday_keys = [(11,30),(12,0),(12,30),(13,0),(13,30)]
            for hh, mm in midday_keys:
                bucket_pct, _ = ucurve_bucket_pct(
                    datetime(2000,1,1,hh,mm, tzinfo=EDT))
                exp15 = int(adv * bucket_pct * 0.5)
                vdu   = int(exp15 * 0.6)
                end_mm = mm + 30; end_hh = hh + (1 if end_mm >= 60 else 0); end_mm %= 60
                myt_hh = (hh + 12) % 24
                myt_eh = (end_hh + 12) % 24
                edt_label = f"{hh:02d}:{mm:02d}–{end_hh:02d}:{end_mm:02d}"
                myt_label = f"{myt_hh:02d}:{mm:02d}–{myt_eh:02d}:{end_mm:02d}"
                print(f"    {edt_label:^20}  {myt_label:^22}  {exp15:>10,}  {vdu:>12,}")
    else:
        adv = atr = None
        print(f"\n  ⚠️  Daily data error: {daily['error']}")

    # ── Execution timeline ──
    print(f"\n  ▌ EXECUTION TIMELINE")
    tl = [
        ("9:30–10:00 AM EDT", "9:30–10:00 PM MYT",   "Run --open → capture IV_open"),
        ("10:00–11:45 AM EDT","10:00–11:45 PM MYT",   "Observe morning fade — no entry"),
        ("11:45 AM–1:30 PM EDT","11:45 PM–1:30 AM MYT","Run --check → Phase 2 GO/NO-GO"),
        ("1:30–3:00 PM EDT",  "1:30–3:00 AM MYT",    "Hold — monitor IV ramp"),
        ("3:00–3:45 PM EDT",  "3:00–3:45 AM MYT",    "Run --exit → harvest ramp"),
        ("3:45 PM EDT",       "3:45 AM MYT",          "⛔ HARD STOP — flat regardless"),
        ("4:00 PM EDT+",      "4:00 AM MYT+",         "Earnings released — IV crush begins"),
    ]
    for edt_t, myt_t, action in tl:
        print(f"    {edt_t:<24} {myt_t:<24} {action}")

    # ── Macro warning ──
    print(f"\n  ⚠️  MACRO CHECK (Gate 6 — user must confirm):")
    print(f"    Verify no same-day CPI / FOMC / PPI / NFP release.")
    print(f"    Source: https://www.investing.com/economic-calendar/")

    # Save to state
    state.update({
        "ticker":               ticker.upper(),
        "earnings_date":        earnings_date,
        "selected_expiry":      target["date"]    if target    else None,
        "selected_dte":         target["dte"]     if target    else None,
        "front_expiry":         front_exp["date"] if front_exp else None,
        "front_expiry_dte":     front_exp["dte"]  if front_exp else None,
        "front_iv_presession":  round(front_iv_pre, 3) if front_iv_pre else None,
        "atm_strike":           atm,
        "adv_20":               adv,
        "atr_20":               atr,
        "iv30":                 iv30,
        "pivot":                daily.get("pivot"),
        "r1":                   daily.get("r1"), "r2": daily.get("r2"),
        "s1":                   daily.get("s1"), "s2": daily.get("s2"),
        "ts_ratio_presession":  round(ts_ratio, 3) if ts_ratio else None,
    })
    save_state(ticker, state)
    print(f"\n  💾 State saved → {_state_path(ticker)}")
    print(f"\n{'═'*65}\n")


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE: OPEN — capture IV_open at 9:30–10:00 AM EDT
# ═══════════════════════════════════════════════════════════════════════════════

def run_open(ticker: str, token: str, state: dict):
    print_header(f"IV_OPEN CAPTURE — {ticker}")

    if not state.get("selected_expiry"):
        print("  ⚠️  No expiry in state — running pre-session check first.")
        run_presession(ticker, token, state)
        state = load_state(ticker)

    metrics = fetch_market_metrics(token, ticker)
    if "error" in metrics:
        print(f"  ❌  {metrics['error']}"); return

    quote = fetch_quote(ticker)
    price = quote.get("price")

    selected_expiry = state["selected_expiry"]
    iv30  = state.get("iv30") or metrics.get("iv30")

    # Near-term IV for the target (trading) expiry
    near_iv = expiry_iv_pct(metrics["expiry_ivs"], selected_expiry)
    if near_iv is None:
        fallback = metrics["iv_index"] * 100 if metrics.get("iv_index") else None
        near_iv  = fallback
        src      = "iv_index (fallback — expiry not in term structure)"
    else:
        src = "Tastytrade per-expiry term structure"

    if near_iv is None:
        print("  ❌  Cannot read near-term IV. Tastytrade API may not have updated yet.")
        print("       Try again in 5 minutes. Market must be open.")
        return

    # Front expiry IV (closest listed — for term structure context)
    front_expiry    = state.get("front_expiry")
    front_expiry_dte= state.get("front_expiry_dte")
    front_iv        = expiry_iv_pct(metrics["expiry_ivs"], front_expiry) if front_expiry else None
    same_exp        = (front_expiry == selected_expiry)

    ts_target = (near_iv  / iv30) if (near_iv  and iv30) else None
    ts_front  = (front_iv / iv30) if (front_iv and iv30) else None
    now_str   = now_edt().isoformat()

    print(f"  Price              : {'${:.2f}'.format(price) if price else 'N/A'}")
    print()
    print(f"  ╔══════════════════════════════════════════╗")
    print(f"  ║  ✅  IV_OPEN (target) = {near_iv:.2f}%  (anchor)       ║")
    if front_iv and not same_exp:
        print(f"  ║     IV_OPEN (front)  = {front_iv:.2f}%  (context)      ║")
    print(f"  ╚══════════════════════════════════════════╝")
    print()
    print(f"  Target expiry      : {selected_expiry}  ({state.get('selected_dte')} DTE)  [{src}]")
    if front_expiry and not same_exp:
        print(f"  Front  expiry      : {front_expiry}  ({front_expiry_dte} DTE)")
    print(f"  IV30               : {f'{iv30:.1f}%' if iv30 else 'N/A'}")
    print(f"  TS Ratio (target)  : {f'{ts_target:.2f}×' if ts_target else 'N/A'}")
    if ts_front and not same_exp:
        print(f"  TS Ratio (front)   : {f'{ts_front:.2f}×' if ts_front else 'N/A'}  (front steeper = more event premium in front)")
    print()
    now_edt_obj = now_edt()
    entry_open  = now_edt_obj.replace(hour=11, minute=45)
    entry_close = now_edt_obj.replace(hour=13, minute=30)
    print(f"  Entry window opens : {edt_myt(entry_open)}")
    print(f"  Entry window closes: {edt_myt(entry_close)}")
    print(f"  Run --check at or after {edt_myt(entry_open)} to evaluate Phase 2 gates.")

    state["iv_open"]           = near_iv
    state["iv_open_timestamp"] = now_str
    state["iv30"]              = iv30
    state["front_iv_open"]     = front_iv   # context only — not used as trade anchor
    state["iv_readings"]       = [{"time": now_str, "iv": near_iv, "price": price}]
    save_state(ticker, state)
    print(f"\n  💾 IV_open saved → {_state_path(ticker)}")
    print(f"\n{'═'*65}\n")


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE: CHECK — Phase 2 gate evaluation (midday)
# ═══════════════════════════════════════════════════════════════════════════════

def run_check(ticker: str, token: str, state: dict):
    now   = now_edt()
    phase = get_phase(now)
    print_header(f"PHASE 2 GATE CHECK — {ticker}")
    print(f"  {PHASE_LABELS.get(phase, phase)}\n")

    iv_open = state.get("iv_open")
    if iv_open is None:
        print("  ❌  IV_open not recorded. Run --open first (9:30–10:00 AM EDT).")
        return

    selected_expiry = state.get("selected_expiry")
    selected_dte    = state.get("selected_dte")
    atm             = state.get("atm_strike")
    adv             = state.get("adv_20")
    atr             = state.get("atr_20")
    iv30            = state.get("iv30")

    # Fresh metrics
    metrics = fetch_market_metrics(token, ticker)
    if "error" in metrics:
        print(f"  ❌  Tastytrade error: {metrics['error']}"); return

    quote  = fetch_quote(ticker)
    price  = quote.get("price")

    near_iv = expiry_iv_pct(metrics["expiry_ivs"], selected_expiry) if selected_expiry else None
    if near_iv is None:
        near_iv = float(metrics["iv_index"]) * 100 if metrics.get("iv_index") else None
    iv_now = near_iv

    if iv_now is None:
        print("  ❌  Cannot read current IV."); return

    # Append reading
    readings: list[dict] = state.get("iv_readings", [])
    readings.append({"time": now.isoformat(), "iv": iv_now, "price": price})
    state["iv_readings"] = readings

    compression = (iv_open - iv_now) / iv_open * 100  # positive = IV has dropped

    # Front expiry IV — for context and enriched Gate 5
    front_expiry     = state.get("front_expiry")
    front_expiry_dte = state.get("front_expiry_dte")
    front_iv_now     = expiry_iv_pct(metrics["expiry_ivs"], front_expiry) if front_expiry else None
    front_iv_open_s  = state.get("front_iv_open")
    same_exp         = (front_expiry == selected_expiry)

    print(f"  IV_open (target) : {iv_open:.2f}%   IV_now : {iv_now:.2f}%   Compression: {compression:.1f}%")
    if front_iv_now and not same_exp:
        fc = f"  ({(front_iv_now-front_iv_open_s)/front_iv_open_s*100:+.1f}% vs open)" if front_iv_open_s else ""
        print(f"  IV_open (front)  : {front_iv_open_s:.2f}%   IV_now : {front_iv_now:.2f}%{fc}  [{front_expiry_dte}d]")
    print(f"  Price   : {'${:.2f}'.format(price) if price else 'N/A'}"
          f"   ATM strike: {'${:.2f}'.format(atm) if atm else 'N/A'}")
    print(f"\n  {'─'*60}")
    print(f"  GATE CHECKS")
    print(f"  {'─'*60}")

    gates: list[tuple] = []   # (num, name, status_sym, detail)

    # ── Gate 1: IV Compression ──────────────────────────────────────────────
    if compression >= 5:
        g1s = "✅"; g1l = f"PASS — {compression:.1f}% drop (strong dip)"
    elif compression >= 3:
        g1s = "✅"; g1l = f"PASS — {compression:.1f}% drop"
    elif compression >= 1:
        g1s = "⚠️ "; g1l = f"BORDERLINE — {compression:.1f}% (shallow; event premium propping IV)"
    else:
        g1s = "❌"; g1l = f"FAIL — {compression:.1f}% (no meaningful dip — skip or wait)"
    gates.append(("1", "IV COMPRESSION",       g1s, g1l))

    # ── Gate 2: IV Based ─────────────────────────────────────────────────────
    if len(readings) >= 2:
        last_two = [r["iv"] for r in readings[-2:]]
        delta_iv = last_two[1] - last_two[0]
        if delta_iv >= -0.5:
            g2s = "✅"; g2l = f"PASS — {last_two[0]:.1f}% → {last_two[1]:.1f}% (stable/rising)"
        else:
            g2s = "❌"; g2l = f"FAIL — {last_two[0]:.1f}% → {last_two[1]:.1f}% (still falling; wait)"
    else:
        g2s = "⚠️ "; g2l = "Only 1 IV reading — re-run --check in 10–15 min to confirm basing"
    gates.append(("2", "IV BASED",             g2s, g2l))

    # ── Gates 3 & 4: Intraday data ──────────────────────────────────────────
    candles = fetch_intraday_candles(ticker)

    def is_morning(c): # 9:30–11:00 EDT
        h = c["time_edt"].hour; m = c["time_edt"].minute
        return (h == 9 and m >= 30) or (h == 10) or (h == 11 and m < 15)

    def is_midday(c):  # 11:30–13:30 EDT
        h = c["time_edt"].hour; m = c["time_edt"].minute
        return (h == 11 and m >= 30) or h == 12 or (h == 13 and m <= 15)

    morning_candles = [c for c in candles if is_morning(c)]
    midday_candles  = [c for c in candles if is_midday(c)]
    morning_avg_rng = (sum(c["candle_range"] for c in morning_candles) / len(morning_candles)
                       if morning_candles else None)

    # Gate 3: VDU + range compression
    vdu_details: list[str] = []
    g3_vol_pass = g3_rng_pass = None
    if adv and len(midday_candles) >= 1:
        last2 = midday_candles[-2:] if len(midday_candles) >= 2 else midday_candles
        vol_results: list[bool] = []
        rng_results: list[bool] = []
        for c in last2:
            exp_vol = expected_15m_vol(adv, c["time_edt"])
            ratio   = c["volume"] / exp_vol if exp_vol else None
            rng_ok  = (morning_avg_rng is not None and
                       c["candle_range"] < morning_avg_rng * 0.5)
            vol_ok  = ratio is not None and ratio < 0.6
            vol_results.append(vol_ok); rng_results.append(rng_ok)
            vol_sym = "✅" if vol_ok else "❌"
            rng_sym = "✅" if rng_ok else "❌"
            _, bucket_label = ucurve_bucket_pct(c["time_edt"])
            vdu_details.append(
                f"    {c['time_edt'].strftime('%H:%M')} EDT  "
                f"vol {int(c['volume']):,} / exp {int(exp_vol):,} ({ratio:.2f}×) {vol_sym}"
                f"  |  range ${c['candle_range']:.2f} {rng_sym}"
                f"  [{bucket_label}]"
            )
        g3_vol_pass = all(vol_results)
        g3_rng_pass = all(rng_results)

        if g3_vol_pass and g3_rng_pass:
            g3s = "✅"; g3l = "PASS — volume dry-up AND range compression"
        elif g3_vol_pass:
            g3s = "⚠️ "; g3l = "PARTIAL — volume drying up, range not compressed"
        elif g3_rng_pass:
            g3s = "⚠️ "; g3l = "PARTIAL — range compressed, volume not low enough"
        else:
            g3s = "❌"; g3l = "FAIL — no VDU, no range compression"
    else:
        g3s = "⚠️ "; g3l = "INSUFFICIENT DATA — fewer than 2 midday candles yet; check again soon"
    gates.append(("3", "VDU + RANGE COMPRESSION", g3s, g3l))

    # Gate 4: Price near S/R + not trending
    s1 = state.get("s1"); r1 = state.get("r1")
    piv = state.get("pivot"); s2 = state.get("s2"); r2 = state.get("r2")
    g4s = "⚠️ "; g4l = "Cannot evaluate — price or S/R not available"

    if price and any(v is not None for v in [s1, r1, piv]):
        levels = {k: v for k, v in
                  [("R2",r2),("R1",r1),("Pivot",piv),("S1",s1),("S2",s2)]
                  if v is not None}
        nearest_k = min(levels, key=lambda k: abs(levels[k] - price))
        nearest_v = levels[nearest_k]
        dist      = abs(price - nearest_v)
        half_atr  = (atr / 2) if atr else None
        close_to_level = bool(half_atr and dist <= half_atr)

        # Trend check: 3+ consecutive directional closes among recent midday candles
        trend = False
        if len(midday_candles) >= 3:
            cls = [c["close"] for c in midday_candles[-3:]]
            if cls[0] < cls[1] < cls[2] or cls[0] > cls[1] > cls[2]:
                trend = True

        if close_to_level and not trend:
            g4s = "✅"
            g4l = (f"PASS — ${price:.2f} within ${dist:.2f} of {nearest_k} "
                   f"(${nearest_v:.2f}); coiling, no trend")
        elif close_to_level and trend:
            g4s = "⚠️ "
            g4l = f"PARTIAL — near {nearest_k} but 3+ directional candles (trending)"
        else:
            g4s = "❌"
            g4l = (f"FAIL — ${price:.2f} is ${dist:.2f} from {nearest_k} "
                   f"(${nearest_v:.2f}); too far from level")
    gates.append(("4", "PRICE NEAR S/R + COILING", g4s, g4l))

    # Gate 5: Term structure inversion — three-point (front / target / IV30)
    iv30_now  = metrics.get("iv30") or iv30
    iv_target = expiry_iv_pct(metrics["expiry_ivs"], selected_expiry) or iv_now
    iv_front  = front_iv_now  # may be None if same expiry or not available
    ts_target = (iv_target / iv30_now) if (iv_target and iv30_now) else None
    ts_front  = (iv_front  / iv30_now) if (iv_front  and iv30_now) else None
    ft_ratio  = (iv_front  / iv_target) if (iv_front and iv_target and not same_exp) else None

    # Gate decision uses target/IV30 ratio (our actual trading tenor)
    if ts_target and ts_target >= 1.15:
        g5s = "✅"; g5l = f"PASS — target steep {ts_target:.2f}×  ({iv_target:.1f}% ÷ {iv30_now:.1f}%)"
    elif ts_target and ts_target >= 1.05:
        g5s = "✅"; g5l = f"PASS — target mild inversion {ts_target:.2f}×"
    elif ts_target and ts_target >= 0.95:
        g5s = "⚠️ "; g5l = f"BORDERLINE — flat {ts_target:.2f}× (weak ramp fuel)"
    else:
        g5s = "❌"; g5l = f"FAIL — no inversion  {f'{ts_target:.2f}×' if ts_target else 'N/A'}"

    if ts_front and not same_exp:
        cal_note = f"  | Front/Target {ft_ratio:.2f}× — calendar viable" if (ft_ratio and ft_ratio >= 1.20) else ""
        g5l += f"  |  front {iv_front:.1f}% ({ts_front:.2f}× IV30){cal_note}"

    gates.append(("5", "TERM STRUCT INVERSION",  g5s, g5l))

    # Gate 6: Macro (manual)
    gates.append(("6", "NO MACRO OVERRIDE", "⚠️ ", "[USER CONFIRM] No CPI/FOMC/PPI/NFP today?"))

    # ── Print gates ──
    for num, name, sym, detail in gates:
        print(f"\n  Gate {num} — {name}")
        print(f"    {sym}  {detail}")
        if num == "3" and vdu_details:
            for line in vdu_details:
                print(line)

    # ── Verdict ──
    confirmed = sum(1 for _, _, s, _ in gates[:5] if s == "✅")
    warned    = sum(1 for _, _, s, _ in gates[:5] if s == "⚠️ ")

    print(f"\n  {'─'*60}")
    if confirmed == 5:
        verdict = "✅  GO  — All 5 confirmed gates pass. Confirm Gate 6 then enter."
        go = True
    elif confirmed >= 4 and warned <= 1:
        verdict = "⚠️   CONDITIONAL GO — 4/5 pass. Confirm Gate 6. Reduce size."
        go = True
    else:
        verdict = f"❌  NO-GO — {confirmed}/5 gates pass. Wait or skip."
        go = False
    print(f"  {verdict}")

    # ── Entry details ──
    if go and price and atm and selected_dte and iv_now:
        T  = selected_dte / 365
        g  = bs_straddle(price, atm, T, iv_now / 100)
        if "error" not in g:
            sc   = g["straddle"]
            vega = g["vega"]
            state["iv_entry"]   = iv_now
            state["entry_cost"] = sc
            state["entry_vega"] = vega
            state["entry_time"] = now.isoformat()
            state["atm_strike"] = atm    # refresh in case price moved

            print(f"\n  ENTRY DETAILS (if Gate 6 confirmed)")
            print(f"    Strike        : ${atm:.2f} ATM")
            print(f"    IV_entry      : ~{iv_now:.1f}%   "
                  f"(IV_open was {iv_open:.1f}%)")
            print(f"    Straddle cost : ${sc:.2f}/share  (${sc*100:.0f}/contract)")
            print(f"    Vega          : ${vega:.3f} per 1 vol pt")
            print(f"    Theta         : ${g['theta_day']:.3f}/day")

            ramp_lo = iv_open * 1.03
            ramp_hi = iv_open * 1.10
            t15 = round(sc * 1.15, 2)
            t25 = round(sc * 1.25, 2)
            hard_stop_edt = now.replace(hour=15, minute=45, second=0)

            print(f"\n  PRE-COMMITTED EXIT LEVELS  (record before order)")
            print(f"    IV target (low)  : ≥ {ramp_lo:.1f}%  (IV_open +3%)")
            print(f"    IV target (high) : ≥ {ramp_hi:.1f}%  (IV_open +10%, fresh intraday high)")
            print(f"    Profit +15%      : close 50% when straddle ≥ ${t15}")
            print(f"    Profit +25%      : close rest when straddle ≥ ${t25}")
            print(f"    Hard time stop   : {edt_myt(hard_stop_edt)} — FULLY FLAT")
            print(f"    Invalidation     : IV drops below {iv_now - 1.0:.1f}% "
                  f"(below trough) → exit immediately")

    # ── Time context ──
    entry_close_edt = now.replace(hour=13, minute=30)
    mins_left       = int((entry_close_edt - now).total_seconds() / 60)
    if phase == "entry-window" and mins_left > 0:
        print(f"\n  ⏱  Entry window closes in {mins_left} min "
              f"({edt_myt(entry_close_edt)})")

    save_state(ticker, state)
    print(f"\n  💾 State updated → {_state_path(ticker)}")
    print(f"\n{'═'*65}\n")


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE: EXIT — Phase 4 trigger monitor
# ═══════════════════════════════════════════════════════════════════════════════

def run_exit(ticker: str, token: str, state: dict):
    now   = now_edt()
    phase = get_phase(now)
    print_header(f"PHASE 4 EXIT MONITOR — {ticker}")
    print(f"  {PHASE_LABELS.get(phase, phase)}\n")

    iv_open  = state.get("iv_open")
    iv_entry = state.get("iv_entry")
    cost     = state.get("entry_cost")
    vega     = state.get("entry_vega")
    selected_expiry = state.get("selected_expiry")
    selected_dte    = state.get("selected_dte")
    atm      = state.get("atm_strike")

    if iv_open is None:
        print("  ❌  IV_open not recorded. Run --open first."); return

    # Fresh IV + price
    metrics = fetch_market_metrics(token, ticker)
    quote   = fetch_quote(ticker)
    price   = quote.get("price")

    iv_now = None
    if "error" not in metrics:
        iv_now = expiry_iv_pct(metrics["expiry_ivs"], selected_expiry) if selected_expiry else None
        if iv_now is None:
            iv_now = float(metrics["iv_index"]) * 100 if metrics.get("iv_index") else None

    # Current straddle estimate (adjust DTE for elapsed time)
    g_now: dict = {}
    if price and atm and selected_dte and iv_now:
        elapsed_days = 0.0
        if state.get("entry_time"):
            try:
                elapsed_days = max(0, (now - datetime.fromisoformat(
                    state["entry_time"])).total_seconds() / 86400)
            except Exception:
                pass
        remaining_dte = max(selected_dte - elapsed_days, 0.01)
        g_now = bs_straddle(price, atm, remaining_dte / 365, iv_now / 100)

    # Front expiry IV for context
    front_expiry     = state.get("front_expiry")
    front_expiry_dte = state.get("front_expiry_dte")
    front_iv_open_e  = state.get("front_iv_open")
    front_iv_now_e   = expiry_iv_pct(metrics["expiry_ivs"], front_expiry) if (
        "error" not in metrics and front_expiry) else None
    same_exp_e = (front_expiry == selected_expiry)

    # ── IV summary ──
    print(f"  {'─'*60}")
    print(f"  IV SUMMARY  (target = trading vehicle  |  front = context)")
    print(f"  {'─'*60}")
    print(f"  TARGET ({selected_expiry}, {selected_dte}d):")
    print(f"    IV_open  : {iv_open:.2f}%")
    print(f"    IV_entry : {f'{iv_entry:.2f}%' if iv_entry else '[not recorded — run --check first]'}")
    print(f"    IV_now   : {f'{iv_now:.2f}%' if iv_now else 'N/A'}")

    if front_iv_now_e and not same_exp_e:
        print(f"  FRONT  ({front_expiry}, {front_expiry_dte}d):")
        print(f"    IV_open  : {f'{front_iv_open_e:.2f}%' if front_iv_open_e else 'N/A'}")
        print(f"    IV_now   : {front_iv_now_e:.2f}%")
        if front_iv_open_e:
            fdiff = front_iv_now_e - front_iv_open_e
            fsym  = "▲" if fdiff > 0 else "▼"
            print(f"    vs open  : {fsym} {abs(fdiff):.2f} vol pts  "
                  f"({'fresh high ✅' if front_iv_now_e > front_iv_open_e else 'below open ❌'})")

    new_high    = bool(iv_now and iv_now > iv_open)
    iv_vs_open  = (iv_now - iv_open) if iv_now else None
    print()
    if iv_vs_open is not None:
        sym = "▲" if iv_vs_open > 0 else "▼"
        flag = "ABOVE IV_open — fresh high ✅ RAMP DELIVERED" if new_high else "below IV_open ❌"
        print(f"  Target IV vs open: {sym} {abs(iv_vs_open):.2f} vol pts  ({flag})")

    if g_now and "error" not in g_now and cost:
        current_val = g_now["straddle"]
        pnl_pct     = (current_val - cost) / cost * 100
        pnl_dollar  = (current_val - cost) * 100
        sym = "▲" if pnl_pct >= 0 else "▼"
        print(f"\n  Est. straddle value : ${current_val:.2f}/share  (entry: ${cost:.2f})")
        print(f"  P&L estimate        : {sym} {abs(pnl_pct):.1f}%  "
              f"({'${:.0f}'.format(pnl_dollar)}/contract)")
        print(f"  [Calculated from live IV + elapsed DTE — verify on live chain]")

    # ── Triggers ──
    hard_stop_edt   = now.replace(hour=15, minute=45)
    mins_to_stop    = max(0, int((hard_stop_edt - now).total_seconds() / 60))

    print(f"\n  {'─'*60}")
    print(f"  PHASE 4 TRIGGERS")
    print(f"  {'─'*60}")

    # T1: IV new high
    if new_high and iv_now:
        t1_sym = "✅ ACTIVE"
        t1_msg = f"IV {iv_now:.1f}% > IV_open {iv_open:.1f}% → SCALE OUT NOW"
    else:
        t1_sym = "⏳ WAITING"
        t1_msg = f"IV {f'{iv_now:.1f}%' if iv_now else '?'} has not cleared IV_open {iv_open:.1f}%"
    print(f"\n  T1  {t1_sym:<16} {t1_msg}")

    # T2: Profit targets
    if cost and g_now and "error" not in g_now:
        current_val = g_now["straddle"]
        pnl_pct     = (current_val - cost) / cost * 100
        t15 = cost * 1.15; t25 = cost * 1.25
        if pnl_pct >= 25:
            t2_sym = "✅ ACTIVE"
            t2_msg = f"+{pnl_pct:.1f}% — close REMAINDER (≥+25% target hit)"
        elif pnl_pct >= 15:
            t2_sym = "✅ ACTIVE"
            t2_msg = f"+{pnl_pct:.1f}% — close 50% now; trail rest at +15% stop"
        else:
            t2_sym = "⏳ WAITING"
            t2_msg = f"{pnl_pct:+.1f}% — targets: +15% (${t15:.2f}) / +25% (${t25:.2f})"
    else:
        t2_sym = "⚠️  NO DATA"; t2_msg = "No entry cost recorded — run --check first"
    print(f"  T2  {t2_sym:<16} {t2_msg}")

    # T3: Time stop
    if mins_to_stop <= 5:
        t3_sym = "🚨 IMMINENT"; t3_msg = f"HARD STOP — {mins_to_stop} min remaining. EXIT NOW."
    elif phase in ("exit-window", "hard-stop"):
        t3_sym = "✅ ACTIVE"
        t3_msg = f"Exit window — {mins_to_stop} min to hard stop ({edt_myt(hard_stop_edt)})"
    else:
        t3_sym = "⏳ WAITING"
        t3_msg = f"Hard stop in {mins_to_stop} min at {edt_myt(hard_stop_edt)}"
    print(f"  T3  {t3_sym:<16} {t3_msg}")

    # T4: Invalidation (IV crush before close)
    crush_threshold = (iv_entry - 1.0) if iv_entry else (iv_open * 0.97)
    if iv_now and iv_now < crush_threshold:
        t4_sym = "🚨 ACTIVE"
        t4_msg = (f"PRE-CLOSE IV CRUSH — IV {iv_now:.1f}% < threshold {crush_threshold:.1f}% "
                  f"→ THESIS BROKEN — EXIT IMMEDIATELY")
    else:
        t4_sym = "✅ INTACT"
        t4_msg = f"No crush. Watch: if IV < {crush_threshold:.1f}% → exit immediately"
    print(f"  T4  {t4_sym:<16} {t4_msg}")

    # ── Recommended action ──
    print(f"\n  {'─'*60}")
    if phase == "hard-stop" or mins_to_stop <= 5:
        print(f"  🛑  ACTION: EXIT NOW — {mins_to_stop} min to hard stop. Do not hold into the print.")
    elif "ACTIVE" in t4_sym:
        print(f"  🚨  ACTION: EXIT IMMEDIATELY — IV crush before close. Thesis broken.")
    elif "ACTIVE" in t1_sym or "ACTIVE" in t2_sym:
        print(f"  💰  ACTION: Exit trigger(s) active — close position (limit at mid or better).")
    elif mins_to_stop <= 20:
        print(f"  ⏰  ACTION: {mins_to_stop} min to hard stop — begin closing now. Don't wait for more.")
    else:
        print(f"  ⏳  HOLD — No trigger active yet. Re-run --exit in 10–15 min.")
        print(f"       IV still needs {(iv_open - (iv_now or 0)):.1f} vol pts to reach IV_open.")

    save_state(ticker, state)
    print(f"\n  💾 State updated → {_state_path(ticker)}")
    print(f"\n{'═'*65}\n")


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="IV Ramp Harvest — Pre-Earnings Same-Day Vega Scalp",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
MYT (UTC+8) execution windows:
  --presession  : any time before market open
  --open        : 9:30–10:00 PM MYT (same day)
  --check       : 11:45 PM–1:30 AM MYT
  --exit        : 3:00–3:45 AM MYT (next day)
  HARD STOP     : 3:45 AM MYT — flat before earnings release at 4:00 AM MYT

Examples:
  python iv_ramp_harvest.py AAPL              # auto-detects phase from clock
  python iv_ramp_harvest.py AAPL --presession # run pre-session baseline any time
  python iv_ramp_harvest.py AAPL --open       # capture IV_open
  python iv_ramp_harvest.py AAPL --check      # midday gate check (run 2–3 times)
  python iv_ramp_harvest.py AAPL --exit       # exit monitor (run every 10–15 min)
  python iv_ramp_harvest.py AAPL --reset      # clear state for new day
        """
    )
    parser.add_argument("ticker",        type=str,            help="Stock ticker symbol")
    parser.add_argument("--presession",  action="store_true", help="Pre-session baseline check")
    parser.add_argument("--open",        action="store_true", help="Capture IV_open at market open")
    parser.add_argument("--check",       action="store_true", help="Midday Phase 2 gate check")
    parser.add_argument("--exit",        action="store_true", help="Afternoon exit monitor")
    parser.add_argument("--reset",       action="store_true", help="Clear saved state for this ticker")
    args = parser.parse_args()

    ticker = args.ticker.upper()

    if args.reset:
        p = _state_path(ticker)
        if os.path.exists(p):
            os.remove(p)
            print(f"✅  State cleared: {p}")
        else:
            print(f"  No state file found for {ticker} ({p})")
        return

    now   = now_edt()
    phase = get_phase(now)
    print(f"\n  Clock  : {edt_myt(now)}")
    print(f"  Phase  : {PHASE_LABELS.get(phase, phase)}")

    state = load_state(ticker)

    # Explicit flag overrides auto-detect
    explicit = args.presession or args.open or args.check or args.exit
    if not explicit:
        # Auto-detect from clock
        if phase == "open-window":
            args.open = True
        elif phase == "entry-window":
            args.check = True
        elif phase in ("exit-window", "hard-stop"):
            args.exit = True
        else:
            args.presession = True

    print("  → Authenticating with Tastytrade...\n")
    token = get_access_token()

    if args.open:
        run_open(ticker, token, state)
    elif args.check:
        run_check(ticker, token, state)
    elif args.exit:
        run_exit(ticker, token, state)
    else:
        run_presession(ticker, token, state)


if __name__ == "__main__":
    main()
