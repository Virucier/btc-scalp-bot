"""Analyse technique multi-timeframe (1h, 4h, 1D) via CCXT.

Usage:
    python crypto_analyzer.py BTC/USDT --exchange binance
"""
import argparse
import json
import os

import ccxt
import numpy as np
import pandas as pd
import requests
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange

TIMEFRAMES = ["1h", "4h", "1d"]
FALLBACK_EXCHANGES = ["binance", "bybit", "okx", "kraken"]
ATR_SL_MULT = 1.5
ATR_TP_MULT = 3.0  # R:R = 1:2


def fetch_ohlcv(exchange, symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.set_index("timestamp")


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    close = df["close"]
    df["rsi"] = RSIIndicator(close, window=14).rsi()
    macd = MACD(close, window_fast=12, window_slow=26, window_sign=9)
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()
    for n in (20, 50, 200):
        df[f"ema{n}"] = EMAIndicator(close, window=n).ema_indicator()
    df["atr"] = AverageTrueRange(df["high"], df["low"], close, window=14).average_true_range()
    return df


def support_resistance(df: pd.DataFrame, window: int = 5, n_levels: int = 3, tol: float = 0.005):
    """Pivots (swing highs/lows) regroupés par proximité, triés par distance au prix."""
    price = df["close"].iloc[-1]
    span = 2 * window + 1
    highs = df["high"][df["high"] == df["high"].rolling(span, center=True).max()].dropna()
    lows = df["low"][df["low"] == df["low"].rolling(span, center=True).min()].dropna()

    def cluster(levels):
        out = []
        for lv in sorted(levels):
            if out and abs(lv - out[-1][-1]) / out[-1][-1] < tol:
                out[-1].append(lv)
            else:
                out.append([lv])
        return [float(np.mean(c)) for c in out]

    pivots = cluster(list(highs) + list(lows))
    supports = sorted([p for p in pivots if p < price], reverse=True)[:n_levels]
    resistances = sorted([p for p in pivots if p > price])[:n_levels]
    return supports, resistances


def timeframe_trend(row) -> tuple[str, int]:
    """Score de tendance: +1 par condition haussière, -1 par condition baissière."""
    score = 0
    score += 1 if row.close > row.ema200 else -1
    score += 1 if row.ema20 > row.ema50 else -1
    score += 1 if row.ema50 > row.ema200 else -1
    score += 1 if row.macd_hist > 0 else -1
    label = "HAUSSIÈRE" if score >= 2 else "BAISSIÈRE" if score <= -2 else "NEUTRE"
    return label, score


def analyze(symbol: str = "BTC/USDT", exchange_id: str = "binance") -> dict:
    exchange = connect(exchange_id, symbol)
    result = {"symbol": symbol, "exchange": exchange.id, "timeframes": {}}
    total = 0
    frames = {}
    for tf in TIMEFRAMES:
        df = add_indicators(fetch_ohlcv(exchange, symbol, tf)).dropna()
        frames[tf] = df
        last = df.iloc[-1]
        label, score = timeframe_trend(last)
        # La tendance de fond (1D, 4h) pèse plus que le 1h
        total += score * {"1h": 1, "4h": 2, "1d": 3}[tf]
        result["timeframes"][tf] = {
            "close": round(last.close, 2),
            "rsi": round(last.rsi, 1),
            "macd": round(last.macd, 2),
            "macd_signal": round(last.macd_signal, 2),
            "macd_hist": round(last.macd_hist, 2),
            "ema20": round(last.ema20, 2),
            "ema50": round(last.ema50, 2),
            "ema200": round(last.ema200, 2),
            "atr": round(last.atr, 2),
            "trend": label,
        }

    supports, resistances = support_resistance(frames["4h"])
    s_d, r_d = support_resistance(frames["1d"])
    result["supports"] = sorted({round(x, 2) for x in supports + s_d}, reverse=True)[:4]
    result["resistances"] = sorted({round(x, 2) for x in resistances + r_d})[:4]

    max_score = 4 * (1 + 2 + 3)
    result["trend_score"] = f"{total}/{max_score}"
    result["global_trend"] = (
        "HAUSSIÈRE" if total >= 0.25 * max_score else "BAISSIÈRE" if total <= -0.25 * max_score else "NEUTRE"
    )

    # Signal d'entrée: tendance globale + momentum 1h/4h (RSI non extrême)
    h1, h4 = result["timeframes"]["1h"], result["timeframes"]["4h"]
    price, atr = h1["close"], h1["atr"]
    side = "NEUTRAL"
    if result["global_trend"] == "HAUSSIÈRE" and h1["macd_hist"] > 0 and h4["rsi"] < 70 and h1["rsi"] < 75:
        side = "LONG"
    elif result["global_trend"] == "BAISSIÈRE" and h1["macd_hist"] < 0 and h4["rsi"] > 30 and h1["rsi"] > 25:
        side = "SHORT"

    result["recommendation"] = {"position": side, "entry": price}
    if side != "NEUTRAL":
        sign = 1 if side == "LONG" else -1
        result["recommendation"].update(
            stop_loss=round(price - sign * ATR_SL_MULT * atr, 2),
            take_profit=round(price + sign * ATR_TP_MULT * atr, 2),
            risk_reward=ATR_TP_MULT / ATR_SL_MULT,
        )
    return result


def get_exchange(exchange_id: str):
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })

    ca_bundle = os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("CURL_CA_BUNDLE")
    if ca_bundle:
        session.verify = ca_bundle

    exchange_class = getattr(ccxt, exchange_id)
    return exchange_class({
        "enableRateLimit": True,
        "session": session,
    })


def connect(exchange_id: str, symbol: str):
    """Tente l'exchange demandé puis les alternatives si indisponible (ex: restriction géographique)."""
    last_err = None
    for eid in [exchange_id] + [e for e in FALLBACK_EXCHANGES if e != exchange_id]:
        try:
            ex = get_exchange(eid)
            ex.fetch_ohlcv(symbol, "1h", limit=2)
            return ex
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[warn] {eid} indisponible: {type(e).__name__}")
    raise RuntimeError(f"Aucun exchange disponible: {last_err}")


def print_report(r: dict) -> None:
    print(f"\n=== {r['symbol']} @ {r['exchange']} ===")
    for tf, d in r["timeframes"].items():
        print(f"[{tf:>2}] close={d['close']} RSI={d['rsi']} MACD_hist={d['macd_hist']} "
              f"EMA20/50/200={d['ema20']}/{d['ema50']}/{d['ema200']} ATR={d['atr']} -> {d['trend']}")
    print(f"Tendance globale : {r['global_trend']} (score {r['trend_score']})")
    print(f"Supports    : {r['supports']}")
    print(f"Résistances : {r['resistances']}")
    rec = r["recommendation"]
    print(f"Recommandation : {rec['position']} (entrée {rec['entry']})")
    if "stop_loss" in rec:
        print(f"  SL={rec['stop_loss']}  TP={rec['take_profit']}  R:R=1:{rec['risk_reward']:.0f}")
    print("Avertissement : outil d'analyse, pas un conseil financier.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("symbol", nargs="?", default="BTC/USDT")
    p.add_argument("--exchange", default="binance")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    res = analyze(a.symbol, a.exchange)
    print(json.dumps(res, indent=2, ensure_ascii=False, default=float) if a.json else "", end="")
    if not a.json:
        print_report(res)
