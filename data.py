import requests
import pandas as pd
import time

COLUMNS = ['open_time', 'open', 'high', 'low', 'close', 'volume']
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def _binance_klines(symbol, interval, limit):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    df = pd.DataFrame(data, columns=[
        'open_time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_asset_volume', 'number_of_trades',
        'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
    ])
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    return df[COLUMNS]


def _bybit_interval(interval):
    mapping = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
               "1h": "60", "2h": "120", "4h": "240", "1d": "D"}
    return mapping.get(interval, interval)


def _bybit_klines(symbol, interval, limit):
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "spot", "symbol": symbol,
              "interval": _bybit_interval(interval), "limit": limit}
    r = requests.get(url, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    payload = r.json()
    if payload.get("retCode") != 0:
        raise RuntimeError(payload.get("retMsg", "Bybit error"))
    rows = list(reversed(payload["result"]["list"]))  # Bybit renvoie du plus récent au plus vieux
    df = pd.DataFrame(rows, columns=['open_time', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
    df['open_time'] = pd.to_datetime(df['open_time'].astype(float), unit='ms')
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    return df[COLUMNS]


def _okx_interval(interval):
    mapping = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
               "1h": "1H", "2h": "2H", "4h": "4H", "1d": "1D"}
    return mapping.get(interval, interval)


def _to_okx_symbol(symbol):
    if "-" in symbol:
        return symbol
    for quote in ("USDT", "USDC", "BTC", "ETH"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[:-len(quote)]}-{quote}"
    return symbol


def _okx_klines(symbol, interval, limit):
    url = "https://www.okx.com/api/v5/market/candles"
    params = {"instId": _to_okx_symbol(symbol), "bar": _okx_interval(interval), "limit": limit}
    r = requests.get(url, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != "0":
        raise RuntimeError(payload.get("msg", "OKX error"))
    rows = list(reversed(payload["data"]))  # OKX renvoie du plus récent au plus vieux
    df = pd.DataFrame(rows, columns=[
        'open_time', 'open', 'high', 'low', 'close', 'volume',
        'volCcy', 'volCcyQuote', 'confirm'
    ])
    df['open_time'] = pd.to_datetime(df['open_time'].astype(float), unit='ms')
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    return df[COLUMNS]


# Ordre de priorité des sources. Binance en premier (si jamais le blocage
# se lève un jour), puis Bybit et OKX qui ne bloquent pas les IP US/EU
# pour la donnée publique de marché.
SOURCES = [
    ("Binance", _binance_klines),
    ("Bybit", _bybit_klines),
    ("OKX", _okx_klines),
]


def get_klines(symbol: str, interval: str, limit: int = 100):
    """
    Récupère des chandeliers OHLCV pour `symbol`/`interval`.
    Essaie Binance, puis Bybit, puis OKX. Retourne un DataFrame trié du plus
    vieux au plus récent avec les colonnes: open_time, open, high, low, close, volume.
    Retourne None si toutes les sources échouent.
    """
    for name, fetch_fn in SOURCES:
        for attempt in range(2):
            try:
                df = fetch_fn(symbol, interval, limit)
                if df is not None and len(df) > 0:
                    print(f"✅ Données récupérées via {name} ({len(df)} chandelles)")
                    return df
            except Exception as e:
                print(f"⚠️ {name} tentative {attempt + 1} échouée: {e}")
                time.sleep(2)
        print(f"❌ {name} indisponible, passage à la source suivante...")

    print("❌ Échec sur toutes les sources de données (Binance, Bybit, OKX)")
    return None
