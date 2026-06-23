#!/usr/bin/env python3
"""
GoldPulse server  —  one always-on worker for a free Linux box.

What it does, on a loop:
  1. Pulls LIVE SPOT XAU/USD from Twelve Data, computes a buy / sell / wait
     signal using the SAME logic as your app's "Auto entry scan"
     (EMA trend + swing support/resistance + ATR-based stops), and pushes
     an ntfy notification *only when the signal changes* (no spam).
  2. Pulls gold-relevant headlines from GDELT, scores them with the same
     keyword model as your app, and pushes a news alert *only when the
     news mood shifts or a high-impact event appears*.

It never hardcodes secrets. Configure with environment variables:
  TWELVE_DATA_KEY   your (regenerated) Twelve Data API key   [required]
  NTFY_TOPIC        your secret ntfy topic name              [required]
  NTFY_SERVER       default https://ntfy.sh                  [optional]
  ANALYSIS_INTERVAL 15min | 30min | 1h | 4h | 1day (default 1h)
  TRADE_STYLE       fast | normal (default normal)
  TRADE_POLL_MINUTES   default 10
  NEWS_POLL_MINUTES    default 20

Run:
  pip install requests
  export TWELVE_DATA_KEY="...."   &&   export NTFY_TOPIC="...."
  python3 goldpulse_server.py
"""

import os
import re
import time
import json
import logging
import requests

# ----------------------------- config ---------------------------------------
TWELVE_KEY        = os.environ.get("97d55b4623f94d98b5d50a3e9929c4bb", "").strip()
NTFY_TOPIC        = os.environ.get("goldpulse-7h3k9x2m-alerts", "").strip()
NTFY_SERVER       = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
SYMBOL            = "XAU/USD"                                   # spot gold
ANALYSIS_INTERVAL = os.environ.get("ANALYSIS_INTERVAL", "1h")
TRADE_STYLE       = os.environ.get("TRADE_STYLE", "normal").lower()
TRADE_POLL_MIN    = int(os.environ.get("TRADE_POLL_MINUTES", "10"))
NEWS_POLL_MIN     = int(os.environ.get("NEWS_POLL_MINUTES", "20"))
STATE_FILE        = os.environ.get("STATE_FILE", "goldpulse_state.json")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("goldpulse")

# --------------------------- indicators --------------------------------------
# (faithful ports of the functions inside your app's index.html)

def ema(values, period):
    if len(values) < period:
        return values[-1] if values else 0.0
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def atr(bars, period):
    if len(bars) < period + 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        trs.append(max(b["high"] - b["low"],
                       abs(b["high"] - p["close"]),
                       abs(b["low"] - p["close"])))
    seg = trs[-period:]
    return sum(seg) / len(seg) if seg else 0.0

def detect_trend(bars):
    closes = [b["close"] for b in bars]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    prev20 = ema(closes[:-5], 20) if len(closes) > 5 else e20
    price = closes[-1]
    if price > e20 and e20 > e50 and e20 >= prev20:
        return "up", "Uptrend"
    if price < e20 and e20 < e50 and e20 <= prev20:
        return "down", "Downtrend"
    return "range", "Range / mixed"

def swing_levels(bars, current):
    recent = bars[-140:]
    lows, highs = [], []
    for i in range(2, len(recent) - 2):
        b = recent[i]
        if (b["low"] <= recent[i - 1]["low"] and b["low"] <= recent[i - 2]["low"]
                and b["low"] <= recent[i + 1]["low"] and b["low"] <= recent[i + 2]["low"]):
            lows.append(b["low"])
        if (b["high"] >= recent[i - 1]["high"] and b["high"] >= recent[i - 2]["high"]
                and b["high"] >= recent[i + 1]["high"] and b["high"] >= recent[i + 2]["high"]):
            highs.append(b["high"])
    supports    = [l for l in lows  if l < current]
    resistances = [h for h in highs if h > current]
    support    = max(supports)    if supports    else min(b["low"]  for b in bars[-60:])
    resistance = min(resistances) if resistances else max(b["high"] for b in bars[-60:])
    return support, resistance

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def build_signal(bars, spot):
    """One actionable signal from the candles + the live spot price."""
    trend_code, trend_label = detect_trend(bars)
    support, resistance = swing_levels(bars, spot)
    vol = atr(bars, 14) or abs(spot * 0.002)
    if not (support < spot):
        support = min(b["low"] for b in bars[-60:])
    if not (resistance > spot):
        resistance = max(b["high"] for b in bars[-60:])

    fast   = (TRADE_STYLE == "fast")
    buffer = clamp(vol * (0.25 if fast else 0.40), 1, 12)
    stop   = clamp(vol * (0.75 if fast else 1.05), 2, 30)
    rr     = 1.5 if fast else 2.0
    rng    = max(0.01, resistance - support)
    pos    = (spot - support) / rng            # 0 = at support, 1 = at resistance

    if trend_code == "up":
        bias, entry = "BUY", support + buffer
        sl, tp = entry - stop, entry + stop * rr
        note = "Pullback-to-support buy. Skip if price closes clearly below support."
    elif trend_code == "down":
        bias, entry = "SELL", resistance - buffer
        sl, tp = entry + stop, entry - stop * rr
        note = "Pullback-to-resistance sell. Skip if price closes clearly above resistance."
    else:
        bias, entry, sl, tp = "WAIT", None, None, None
        note = "Range / no clear trend. Better entries form near support or resistance."

    return {
        "bias": bias, "trend": trend_label, "spot": spot,
        "support": support, "resistance": resistance,
        "entry": entry, "sl": sl, "tp": tp, "rr": rr,
        "pos_pct": round(pos * 100), "mid_caution": 0.35 < pos < 0.65,
        "note": note,
    }

# ------------------------- news scoring --------------------------------------
# (faithful port of scoreNewsText from your app)

_BUY_RULES = [
    (r"weak(er)?\s+(us\s+)?dollar|dollar\s+(falls|drops|slips|weakens)|dxy\s+(falls|drops|slips)", 4),
    (r"treasury\s+yields?\s+(fall|drop|slip|ease)|yields?\s+(fall|drop|slip|ease)", 4),
    (r"fed\s+(dovish|cuts?|rate\s+cut)|rate\s+cut\s+(bets|hopes|expectations)|lower\s+rates?", 4),
    (r"weak\s+(jobs|payrolls|employment)|nfp\s+(miss|misses|weaker)|unemployment\s+(rises|higher)", 3),
    (r"recession|slowdown|banking\s+stress|risk\s+off|safe\s+haven|safe-haven", 3),
    (r"war|conflict|missile|attack|geopolitical|middle\s+east|ukraine|iran|israel|tariff\s+tension", 3),
]
_SELL_RULES = [
    (r"strong(er)?\s+(us\s+)?dollar|dollar\s+(rises|jumps|gains|strengthens)|dxy\s+(rises|jumps|gains)", 4),
    (r"treasury\s+yields?\s+(rise|jump|climb|surge)|yields?\s+(rise|jump|climb|surge)", 4),
    (r"fed\s+(hawkish|higher\s+for\s+longer)|rate\s+hike|hikes?|less\s+rate\s+cuts?|reduced\s+rate\s+cut\s+bets", 4),
    (r"hot(ter)?\s+(cpi|inflation)|inflation\s+(beats|above|higher\s+than|rises)|cpi\s+(beats|above|higher)", 3),
    (r"strong\s+(jobs|payrolls|employment)|nfp\s+(beats|stronger)|unemployment\s+(falls|lower)", 3),
    (r"ceasefire|de-escalation|risk\s+appetite|stocks\s+rally|safe\s+haven\s+demand\s+(eases|falls)", 2),
]
_RISK_RULES = [
    (r"cpi|inflation|pce|ppi", 20),
    (r"nfp|nonfarm|payrolls|jobs|unemployment|jobless", 20),
    (r"fomc|fed|powell|rate\s+decision|central\s+bank", 25),
    (r"treasury\s+yields?|dxy|dollar", 12),
    (r"war|conflict|attack|missile|geopolitical|iran|israel|ukraine", 18),
]

def news_bias(text):
    t = " " + (text or "").lower() + " "
    buy  = sum(w for p, w in _BUY_RULES  if re.search(p, t))
    sell = sum(w for p, w in _SELL_RULES if re.search(p, t))
    risk = sum(w for p, w in _RISK_RULES if re.search(p, t))
    risk = min(100, risk + min(25, (buy + sell) * 4))
    if buy >= sell + 3:
        bias = "BUY-SIDE"
    elif sell >= buy + 3:
        bias = "SELL-SIDE"
    else:
        bias = "NEUTRAL"
    risk_label = "HIGH" if risk >= 70 else "MEDIUM" if risk >= 35 else "LOW"
    return bias, buy, sell, risk_label

# --------------------------- data sources ------------------------------------

def fetch_candles(interval, n=200):
    r = requests.get("https://api.twelvedata.com/time_series",
                     params={"symbol": SYMBOL, "interval": interval,
                             "outputsize": n, "apikey": TWELVE_KEY, "format": "JSON"},
                     timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"Twelve Data: {data.get('message', data)}")
    vals = list(reversed(data.get("values", [])))     # API returns newest-first
    bars = []
    for v in vals:
        try:
            bars.append({"open": float(v["open"]), "high": float(v["high"]),
                         "low": float(v["low"]), "close": float(v["close"])})
        except (KeyError, ValueError, TypeError):
            continue
    return bars

def fetch_spot():
    """Live spot XAU/USD price (not a candle close, not futures)."""
    r = requests.get("https://api.twelvedata.com/price",
                     params={"symbol": SYMBOL, "apikey": TWELVE_KEY}, timeout=15)
    r.raise_for_status()
    return float(r.json()["price"])

_NEWS_QUERY = ('("gold" OR "XAUUSD" OR "spot gold" OR "Federal Reserve" OR '
               '"US dollar" OR "Treasury yields" OR "CPI" OR "NFP" OR '
               '"nonfarm payrolls" OR "PCE inflation")')

def fetch_news():
    """GDELT's free server rate-limits (429) and is sometimes slow; retry a few
    times with backoff, then skip this cycle quietly rather than spamming errors."""
    params = {"query": _NEWS_QUERY, "mode": "ArtList", "format": "json",
              "timespan": "2h", "maxrecords": 20, "sort": "DateDesc"}
    for attempt in range(3):
        try:
            r = requests.get("https://api.gdeltproject.org/api/v2/doc/doc",
                             params=params, headers={"User-Agent": "GoldPulse/1.0"},
                             timeout=25)
            if r.status_code == 429:
                wait = 10 * (attempt + 1)
                log.warning("news source busy (429); retrying in %ss", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json().get("articles", [])
        except requests.RequestException as e:
            log.warning("news fetch attempt %d failed: %s", attempt + 1, e)
            time.sleep(5)
    log.info("news: source unavailable, skipping this cycle")
    return []

# ------------------------------ ntfy -----------------------------------------

def ntfy(title, message, priority="default", tags=None):
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        requests.post(f"{NTFY_SERVER}/{NTFY_TOPIC}",
                      data=message.encode("utf-8"), headers=headers, timeout=15)
        log.info("ntfy -> %s", title)
    except Exception as e:
        log.error("ntfy failed: %s", e)

# ----------------------------- state -----------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"last_bias": None, "last_news_bias": None, "last_news_risk": None}

def save_state(s):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f)
    except Exception as e:
        log.error("state save failed: %s", e)

# ----------------------------- tasks -----------------------------------------

def do_trade(state):
    bars = fetch_candles(ANALYSIS_INTERVAL)
    if len(bars) < 60:
        log.warning("only %d candles, skipping", len(bars))
        return
    try:
        spot = fetch_spot()
    except Exception:
        spot = bars[-1]["close"]          # fallback: latest spot candle close
    sig = build_signal(bars, spot)

    if sig["bias"] == state.get("last_bias"):
        log.info("trade: %s unchanged, spot %.2f", sig["bias"], spot)
        return

    if sig["bias"] == "WAIT":
        ntfy("GoldPulse: now WAIT (no clear trend)",
             f"Spot ${spot:,.2f}\nTrend: {sig['trend']}\n"
             f"Support ${sig['support']:,.2f} / Resistance ${sig['resistance']:,.2f}\n"
             "No clear setup — stand aside.",
             priority="default", tags=["pause_button"])
    else:
        dist = abs(spot - sig["entry"])
        near = dist <= max(2.0, abs(sig["entry"] - sig["sl"]) * 0.5)
        zone = ("price is near the entry zone now — watch for the trigger" if near
                else "price is away from the entry — wait for a pullback toward it")
        arrow = "chart_with_upwards_trend" if sig["bias"] == "BUY" else "chart_with_downwards_trend"
        ntfy(f"GoldPulse: {sig['bias']} setup (Gold)",
             f"Spot ${spot:,.2f}   ({sig['trend']})\n"
             f"Entry ${sig['entry']:,.2f}\n"
             f"Stop ${sig['sl']:,.2f}\n"
             f"Target ${sig['tp']:,.2f}   ({sig['rr']:.1f}:1)\n"
             f"Support ${sig['support']:,.2f} / Resistance ${sig['resistance']:,.2f}\n"
             f"{zone}.\n{sig['note']}\n"
             "(GoldPulse alert — not financial advice)",
             priority="high", tags=[arrow])

    state["last_bias"] = sig["bias"]
    save_state(state)

def do_news(state):
    arts = fetch_news()
    titles = [(a.get("title") or "").strip() for a in arts]
    titles = [t for t in titles if t]
    if not titles:
        return
    bias, buy, sell, risk = news_bias(" | ".join(titles[:15]))   # aggregate mood

    changed = ((bias != state.get("last_news_bias") and bias != "NEUTRAL")
               or (risk == "HIGH" and state.get("last_news_risk") != "HIGH"))
    if changed:
        top = []
        for a in arts[:6]:
            t = (a.get("title") or "").strip()
            if t:
                top.append(f"\u2022 {t}  ({a.get('domain') or 'news'})")
        ntfy(f"GoldPulse news: {bias} (risk {risk})",
             "Gold-related news mood has shifted.\n"
             f"Bias: {bias}   |   News risk: {risk}\n\n"
             "Recent headlines:\n" + "\n".join(top) +
             "\n\nNews is a filter, not a signal — confirm on the chart. "
             "Avoid fresh entries during high-impact releases.",
             priority="high" if risk == "HIGH" else "default", tags=["newspaper"])

    state["last_news_bias"] = bias
    state["last_news_risk"] = risk
    save_state(state)

# ------------------------------ main -----------------------------------------

def main():
    if not TWELVE_KEY or not NTFY_TOPIC:
        raise SystemExit("Set TWELVE_DATA_KEY and NTFY_TOPIC environment variables first.")
    log.info("GoldPulse starting — %s %s, trade=%dm news=%dm",
             SYMBOL, ANALYSIS_INTERVAL, TRADE_POLL_MIN, NEWS_POLL_MIN)
    state = load_state()
    ntfy("GoldPulse started",
         f"Watching spot {SYMBOL} ({ANALYSIS_INTERVAL}). "
         "You'll get a ping when a setup or a news shift appears.",
         priority="low", tags=["satellite_antenna"])

    next_trade = next_news = 0.0
    while True:
        now = time.time()
        if now >= next_trade:
            try:
                do_trade(state)
            except Exception as e:
                log.error("trade error: %s", e)
            next_trade = now + TRADE_POLL_MIN * 60
        if now >= next_news:
            try:
                do_news(state)
            except Exception as e:
                log.error("news error: %s", e)
            next_news = now + NEWS_POLL_MIN * 60
        time.sleep(20)

if __name__ == "__main__":
    main()
