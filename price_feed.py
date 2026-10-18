"""Flux de prix temps réel.

- WebSocket (Gate.io : spot.book_ticker + spot.trades ; KuCoin : ticker + match) avec reconnexion.
  L'abonnement peut être pris AVANT le listing : les deux exchanges acceptent un abonnement
  sur une paire pas encore cotée (voir README du rapport pour les limites vérifiées).
- Repli REST polling (Gate.io, KuCoin, OKX), flux simulé `mock`, rejeu d'un enregistrement.
- `TickRecorder` : sauvegarde chaque tick (JSON) pour backtest.
Les générateurs peuvent émettre `None` (battement de ~1 s sans tick) afin que l'appelant
puisse appliquer ses timeouts même quand le marché est muet.
"""
import json
import os
import random
import time
from pathlib import Path
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
    price: float                         # trade: prix exécuté ; book: milieu bid/ask
    recv_ts: float                       # time.time() local à la réception
    exch_ts: Optional[float] = None      # timestamp de l'exchange (s) si fourni
    volume: float = 0.0                  # taille du trade (0 pour une mise à jour du carnet)
    kind: str = "price"                  # "book" | "trade" | "price"
    bid: Optional[float] = None
    ask: Optional[float] = None


@dataclass
class FeedStats:
    mode: str = ""
    ticks: int = 0
    reconnects: int = 0
    max_gap: float = 0.0
    lat_ms: list = field(default_factory=list)   # recv - exch (dépend de l'horloge locale)
    _last: float = 0.0
    first_tick_ts: float = 0.0
    ws_ready_ms: float = 0.0           # connexion + ack d'abonnement

    def record(self, t: Tick) -> None:
        self.ticks += 1
        self.first_tick_ts = self.first_tick_ts or t.recv_ts
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
        yield Tick(p, time.time()) if p is not None else None
        time.sleep(max(0.0, interval - (time.time() - t0)))


# ---------------------------------------------------------------- WebSocket
RESUB_EVERY = 5.0        # ré-abonnement tant qu'aucun tick n'est reçu (abonnement « fantôme » avant listing)


def _f(x) -> Optional[float]:
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _ws_gate(symbol: str, stats: Optional["FeedStats"] = None) -> Iterator[Optional[Tick]]:
    """Gate.io : spot.book_ticker (meilleur bid/ask, push à chaque changement) + spot.trades."""
    pair = pair_id("gate", symbol)
    t_conn = time.time()
    ws = websocket.create_connection(os.getenv("GATE_WS_URL", "wss://api.gateio.ws/ws/v4/"), timeout=10, sslopt=_sslopt())

    def sub(channel):
        ws.send(json.dumps({"time": int(time.time()), "channel": channel, "event": "subscribe", "payload": [pair]}))

    try:
        sub("spot.book_ticker")
        sub("spot.trades")
        ws.settimeout(1)
        last_ping = last_resub = last_trades_retry = time.time()
        trades_ok, got_tick, acked = False, False, False
        while True:
            now = time.time()
            if now - last_ping > 10:
                ws.send(json.dumps({"time": int(now), "channel": "spot.ping"}))
                last_ping = now
            if not got_tick and now - last_resub > RESUB_EVERY:
                sub("spot.book_ticker")
                last_resub = now
            if not trades_ok and now - last_trades_retry > 2:     # trades refuse une paire inconnue : on réessaie
                sub("spot.trades")
                last_trades_retry = now
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                yield None
                continue
            ch, ev = msg.get("channel"), msg.get("event")
            if ev == "subscribe":
                if msg.get("error"):
                    if ch == "spot.book_ticker":
                        raise RuntimeError(f"gate ws: {msg['error']}")
                    continue                                       # trades: paire pas encore connue
                if ch == "spot.trades":
                    trades_ok = True
                if not acked and stats is not None:
                    stats.ws_ready_ms, acked = (time.time() - t_conn) * 1000, True
                continue
            if ev != "update":
                continue
            res = msg["result"]
            if ch == "spot.book_ticker":
                bid, ask = _f(res.get("b")), _f(res.get("a"))
                if bid and ask:
                    got_tick = True
                    yield Tick((bid + ask) / 2, time.time(), (res.get("t") or 0) / 1000 or None,
                               0.0, "book", bid, ask)
            elif ch == "spot.trades":
                for tr in (res if isinstance(res, list) else [res]):
                    price = _f(tr.get("price"))
                    if price:
                        got_tick = True
                        yield Tick(price, time.time(), (float(tr.get("create_time_ms") or 0)) / 1000 or None,
                                   float(tr.get("amount") or 0), "trade")
    finally:
        ws.close()


def _ws_kucoin(symbol: str, stats: Optional["FeedStats"] = None) -> Iterator[Optional[Tick]]:
    """KuCoin : /market/ticker (meilleur bid/ask) + /market/match (trades)."""
    pair = pair_id("kucoin", symbol)
    t_conn = time.time()
    info = HTTP.post("https://api.kucoin.com/api/v1/bullet-public", timeout=REST_TIMEOUT).json()["data"]
    srv = info["instanceServers"][0]
    ping_every = srv["pingInterval"] / 1000 * 0.8
    ws = websocket.create_connection(f"{srv['endpoint']}?token={info['token']}", timeout=10, sslopt=_sslopt())

    def sub():
        for topic in (f"/market/ticker:{pair}", f"/market/match:{pair}"):
            ws.send(json.dumps({"id": int(time.time() * 1000), "type": "subscribe", "topic": topic, "response": True}))

    try:
        sub()
        ws.settimeout(1)
        last_ping = last_resub = time.time()
        got_tick, acked = False, False
        while True:
            now = time.time()
            if now - last_ping > ping_every:
                ws.send(json.dumps({"id": int(now * 1000), "type": "ping"}))
                last_ping = now
            if not got_tick and now - last_resub > RESUB_EVERY:
                sub()
                last_resub = now
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                yield None
                continue
            if msg.get("type") == "ack" and not acked and stats is not None:
                stats.ws_ready_ms, acked = (time.time() - t_conn) * 1000, True
            if msg.get("type") == "error":
                raise RuntimeError(f"kucoin ws: {msg.get('data')}")
            d = msg.get("data") or {}
            if msg.get("subject") == "trade.ticker":
                bid, ask, price = _f(d.get("bestBid")), _f(d.get("bestAsk")), _f(d.get("price"))
                if price:
                    got_tick = True
                    yield Tick(price, time.time(), d["time"] / 1000 if d.get("time") else None,
                               0.0, "book", bid, ask)
            elif msg.get("subject") == "trade.l3match" and _f(d.get("price")):
                got_tick = True
                yield Tick(float(d["price"]), time.time(), None, float(d.get("size") or 0), "trade")
    finally:
        ws.close()


_WS = {"gate": _ws_gate, "kucoin": _ws_kucoin}


def stream_ws(exchange: str, symbol: str, stats: FeedStats) -> Iterator[Optional[Tick]]:
    """WebSocket avec reconnexion (backoff 1→8 s) ; lève RuntimeError après MAX_WS_FAILURES échecs d'affilée."""
    failures, delay = 0, 1.0
    while True:
        try:
            for tick in _WS[exchange](symbol, stats):
                if tick is not None:
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
    elif mode == "replay":
        stats.mode = "replay"
        src = iter(())      # remplacé par stream_replay() côté appelant
    elif mode in ("auto", "ws") and exchange in WS_EXCHANGES:
        stats.mode = "websocket"
        src = _with_fallback(exchange, symbol, interval, stats, allow_fallback=(mode == "auto"))
    else:
        stats.mode = f"rest({interval}s)"
        src = stream_rest(exchange, symbol, interval)
    for tick in src:
        if tick is not None:
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


# ---------------------------------------------------------------- Enregistrement / rejeu
class TickRecorder:
    """Écrit chaque tick dans un tableau JSON (flush régulier ; fermé proprement par close()).
    Champs : ts_ms (réception locale), exch_ts_ms, price, volume, kind, bid, ask."""

    def __init__(self, path: str, meta: Optional[dict] = None):
        self.f = open(path, "w", encoding="utf-8")
        self.n = 0
        self._flushed = time.time()
        self.f.write("[\n")
        if meta:
            self.f.write(json.dumps({"meta": meta}) + ",\n")

    def write(self, t: Tick) -> None:
        row = {"ts_ms": round(t.recv_ts * 1000, 3), "exch_ts_ms": round(t.exch_ts * 1000, 3) if t.exch_ts else None,
               "price": t.price, "volume": t.volume, "kind": t.kind, "bid": t.bid, "ask": t.ask}
        self.f.write(json.dumps(row) + ",\n")
        self.n += 1
        if time.time() - self._flushed > 1:
            self.f.flush()
            self._flushed = time.time()

    def close(self) -> None:
        if not self.f.closed:
            self.f.write("{}\n]\n")      # entrée vide finale : évite la virgule pendante
            self.f.close()


def load_ticks(path: str) -> list:
    """Charge un enregistrement ligne par ligne (une ligne = un tick) : tolère un fichier tronqué par un crash."""
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue                    # dernière ligne coupée
        if r.get("price"):
            rows.append(r)
    return rows


def stream_replay(path: str, speed: float = 1.0) -> Iterator[Tick]:
    """Rejoue un enregistrement en respectant les écarts temporels (speed=0 : instantané)."""
    rows = load_ticks(path)
    prev = None
    for r in rows:
        if prev is not None and speed > 0:
            time.sleep(max(0.0, (r["ts_ms"] - prev) / 1000 / speed))
        prev = r["ts_ms"]
        yield Tick(r["price"], r["ts_ms"] / 1000, (r["exch_ts_ms"] or 0) / 1000 or None,   # recv_ts d'origine : latence/silences fidèles
                   r.get("volume") or 0.0,
                   r.get("kind") or "price", r.get("bid"), r.get("ask"))
