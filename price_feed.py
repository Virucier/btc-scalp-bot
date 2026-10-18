"""Flux de prix temps réel : WebSocket (Gate.io, KuCoin) avec reconnexion, repli REST polling
(Gate.io, KuCoin, OKX), et flux simulé (`mock`) pour tester sans marché.
"""
import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

import requests
import websocket

REST_TIMEOUT = 5
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
WS_EXCHANGES = ("gate", "kucoin")
REST_EXCHANGES = ("gate", "kucoin", "okx")
MAX_WS_FAILURES = 3          # échecs consécutifs avant repli sur REST


@dataclass
class Tick:
    price: float
    recv_ts: float                       # time.time() local à la réception
    exch_ts: Optional[float] = None      # timestamp de l'exchange (s) si fourni


@dataclass
class FeedStats:
    mode: str = ""
    ticks: int = 0
    reconnects: int = 0
    max_gap: float = 0.0
    lat_ms: list = field(default_factory=list)   # recv - exch (dépend de l'horloge locale)
    _last: float = 0.0

    def record(self, t: Tick) -> None:
        self.ticks += 1
        if self._last:
            self.max_gap = max(self.max_gap, t.recv_ts - self._last)
        self._last = t.recv_ts
        if t.exch_ts:
            self.lat_ms.append((t.recv_ts - t.exch_ts) * 1000)


def _http() -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    ca = os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("CURL_CA_BUNDLE")
    if ca:
        s.verify = ca
    return s


HTTP = _http()


def _sslopt() -> dict:
    ca = os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("SSL_CERT_FILE")
    return {"ca_certs": ca} if ca else {}


def pair_id(exchange: str, symbol: str, quote: str = "USDT") -> str:
    sep = "_" if exchange == "gate" else "-"
    return f"{symbol.upper()}{sep}{quote}"


# ---------------------------------------------------------------- REST
def rest_price(exchange: str, symbol: str) -> Optional[float]:
    """Dernier prix, ou None si la paire n'existe pas encore / indisponible."""
    pid = pair_id(exchange, symbol)
    try:
        if exchange == "gate":
            r = HTTP.get("https://api.gateio.ws/api/v4/spot/tickers", params={"currency_pair": pid}, timeout=REST_TIMEOUT)
            data = r.json() if r.ok else []
            return float(data[0]["last"]) if data else None
        if exchange == "kucoin":
            d = HTTP.get("https://api.kucoin.com/api/v1/market/orderbook/level1", params={"symbol": pid},
                         timeout=REST_TIMEOUT).json().get("data")
            return float(d["price"]) if d and d.get("price") else None
        if exchange == "okx":
            d = HTTP.get("https://www.okx.com/api/v5/market/ticker", params={"instId": pid},
                         timeout=REST_TIMEOUT).json().get("data")
            return float(d[0]["last"]) if d else None
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None
    raise ValueError(f"Exchange non supporté: {exchange} (choix: {', '.join(REST_EXCHANGES)})")


def wait_until_listed(exchange: str, symbol: str, poll: float = 0.5, timeout: Optional[float] = None,
                      log=print) -> Optional[float]:
    """Sonde en REST jusqu'à ce que la paire cote ; retourne le premier prix (None si timeout)."""
    t0, last_log = time.time(), 0.0
    while True:
        p = rest_price(exchange, symbol)
        if p is not None:
            return p
        now = time.time()
        if timeout and now - t0 > timeout:
            return None
        if now - last_log > 30:
            log(f"… {pair_id(exchange, symbol)} pas encore listée sur {exchange} (sonde toutes les {poll}s)")
            last_log = now
        time.sleep(poll)


def stream_rest(exchange: str, symbol: str, interval: float = 0.5) -> Iterator[Tick]:
    while True:
        t0 = time.time()
        p = rest_price(exchange, symbol)
        if p is not None:
            yield Tick(p, time.time())
        time.sleep(max(0.0, interval - (time.time() - t0)))


# ---------------------------------------------------------------- WebSocket
def _ws_gate(symbol: str) -> Iterator[Tick]:
    ws = websocket.create_connection("wss://api.gateio.ws/ws/v4/", timeout=10, sslopt=_sslopt())
    try:
        ws.send(json.dumps({"time": int(time.time()), "channel": "spot.tickers", "event": "subscribe",
                            "payload": [pair_id("gate", symbol)]}))
        ws.settimeout(1)
        last_ping = time.time()
        while True:
            if time.time() - last_ping > 10:
                ws.send(json.dumps({"time": int(time.time()), "channel": "spot.ping"}))
                last_ping = time.time()
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if msg.get("error"):
                raise RuntimeError(f"gate ws: {msg['error']}")
            if msg.get("channel") == "spot.tickers" and msg.get("event") == "update":
                res = msg["result"]
                yield Tick(float(res["last"]), time.time(), (msg.get("time_ms") or 0) / 1000 or None)
    finally:
        ws.close()


def _ws_kucoin(symbol: str) -> Iterator[Tick]:
    info = HTTP.post("https://api.kucoin.com/api/v1/bullet-public", timeout=REST_TIMEOUT).json()["data"]
    srv = info["instanceServers"][0]
    ping_every = srv["pingInterval"] / 1000 * 0.8
    ws = websocket.create_connection(f"{srv['endpoint']}?token={info['token']}", timeout=10, sslopt=_sslopt())
    try:
        ws.send(json.dumps({"id": int(time.time() * 1000), "type": "subscribe",
                            "topic": f"/market/ticker:{pair_id('kucoin', symbol)}", "response": True}))
        ws.settimeout(1)
        last_ping = time.time()
        while True:
            if time.time() - last_ping > ping_every:
                ws.send(json.dumps({"id": int(time.time() * 1000), "type": "ping"}))
                last_ping = time.time()
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if msg.get("type") == "error":
                raise RuntimeError(f"kucoin ws: {msg.get('data')}")
            if msg.get("subject") == "trade.ticker":
                d = msg["data"]
                yield Tick(float(d["price"]), time.time(), d["time"] / 1000 if d.get("time") else None)
    finally:
        ws.close()


_WS = {"gate": _ws_gate, "kucoin": _ws_kucoin}


def stream_ws(exchange: str, symbol: str, stats: FeedStats) -> Iterator[Tick]:
    """WebSocket avec reconnexion (backoff 1→8 s) ; lève RuntimeError après MAX_WS_FAILURES échecs d'affilée."""
    failures, delay = 0, 1.0
    while True:
        try:
            for tick in _WS[exchange](symbol):
                failures, delay = 0, 1.0
                yield tick
        except (websocket.WebSocketException, OSError, RuntimeError, ValueError, KeyError,
                requests.RequestException) as e:
            failures += 1
            stats.reconnects += 1
            print(f"[feed] WS {exchange} coupé ({type(e).__name__}: {str(e)[:80]}) — essai {failures}/{MAX_WS_FAILURES}")
            if failures >= MAX_WS_FAILURES:
                raise RuntimeError("WebSocket indisponible") from e
            time.sleep(delay)
            delay = min(delay * 2, 8)


# ---------------------------------------------------------------- Mock
def stream_mock(start_price: float = 1.0, interval: float = 0.05, seed: int = 7) -> Iterator[Tick]:
    """Pump de listing simulé : spike, dump, rebond, puis repli (déclenche TP1, TP2 et le trailing)."""
    rng = random.Random(seed)
    price = start_price
    legs = [(2.3, 40), (2.08, 10), (4.4, 40), (4.12, 8), (6.2, 30), (3.9, 40)]  # (cible x, nb de ticks)
    for target_x, n in legs:
        target = start_price * target_x   # cible = multiple du prix de départ
        step = (target / price) ** (1 / n)
        for _ in range(n):
            price *= step * (1 + rng.uniform(-0.01, 0.01))
            yield Tick(price, time.time())
            time.sleep(interval)


# ---------------------------------------------------------------- Façade
def stream_prices(exchange: str, symbol: str, mode: str = "auto", interval: float = 0.5,
                  stats: Optional[FeedStats] = None) -> Iterator[Tick]:
    """mode: auto (WS puis repli REST) | ws | rest | mock."""
    stats = stats or FeedStats()
    if exchange == "mock" or mode == "mock":
        stats.mode = "mock"
        src = stream_mock(interval=interval)
    elif mode in ("auto", "ws") and exchange in WS_EXCHANGES:
        stats.mode = "websocket"
        src = _with_fallback(exchange, symbol, interval, stats, allow_fallback=(mode == "auto"))
    else:
        stats.mode = f"rest({interval}s)"
        src = stream_rest(exchange, symbol, interval)
    for tick in src:
        stats.record(tick)
        yield tick


def _with_fallback(exchange, symbol, interval, stats, allow_fallback):
    try:
        yield from stream_ws(exchange, symbol, stats)
    except RuntimeError:
        if not allow_fallback:
            raise
        stats.mode = f"rest({interval}s) [repli après échec WS]"
        print(f"[feed] repli sur REST polling ({interval}s)")
        yield from stream_rest(exchange, symbol, interval)
