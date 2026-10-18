"""Listing Scanner & Safety Filter.

Module 1 - détection des listings à venir (KuCoin annonces, Gate.io paires futures)
Module 2 - audit anti-scam d'un contrat (GoPlus Security + DexScreener)
Module 3 - plan de trade listing (TP1 +100 %, TP2 +300 %, trailing stop serré)

Usage:
    python listing_sniper.py scan [--days 1] [--recent 7] [--notify] [--dry-run]
    python listing_sniper.py watch [--interval 300] [--days 1] [--dry-run]
    python listing_sniper.py monitor <gate|kucoin|okx|mock> <SYMBOLE> [--mode auto|ws|rest] [--size 100]
                                     [--slippage 5] [--record ticks.json] [--wait-timeout N]
    python listing_sniper.py monitor replay <ticks.json> [--speed 10]    # backtest sur un enregistrement
    python listing_sniper.py hunter [--lead 300] [--post 30] [--record-dir ticks] [--notify]   # chasse automatique
    python listing_sniper.py monitor ... --contract 0x… --chain 1      # audit GoPlus/DexScreener obligatoire avant l'entrée
    python listing_sniper.py preflight [--record-dir ticks]          # à lancer sur le serveur avant le hunter
    python listing_sniper.py analyze_ticks <ticks.json> [--json]     # spread, hausse 1re seconde, volume 1re minute
    python listing_sniper.py audit <chain_id> <adresse_contrat>
    python listing_sniper.py simulate

Outil d'analyse uniquement : aucun ordre n'est passé. Les listings sont extrêmement
volatils et le risque de perte totale est réel.
"""
import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HTTP_TIMEOUT = 15
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    ca = os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("CURL_CA_BUNDLE")
    if ca:
        s.verify = ca
    return s


HTTP = _session()


def _get_json(url: str, **params):
    r = HTTP.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Module 1 - Détection des listings
# ---------------------------------------------------------------------------
@dataclass
class Listing:
    exchange: str
    market: str            # "spot" | "futures"
    symbol: str
    start: datetime        # UTC
    time_known: bool       # False si l'annonce ne donne que la date
    title: str = ""
    url: str = ""


_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}
_RE_TIME_DATE = re.compile(r"(\d{1,2}):(\d{2}) on (\w+) (\d{1,2}), (\d{4})")
_RE_DATE_ONLY = re.compile(r"on (\w+) (\d{1,2}), (\d{4})")
_RE_ISO = re.compile(r"\((\d{4})-(\d{2})-(\d{2})\)")


def _parse_kucoin_start(title: str, desc: str, ctime_ms: int):
    """Retourne (datetime UTC, heure_connue)."""
    m = _RE_TIME_DATE.search(desc)
    if m and m.group(3) in _MONTHS:
        h, mi, mon, d, y = m.groups()
        return datetime(int(y), _MONTHS[mon], int(d), int(h), int(mi), tzinfo=timezone.utc), True
    m = _RE_DATE_ONLY.search(desc)
    if m and m.group(1) in _MONTHS:
        mon, d, y = m.groups()
        return datetime(int(y), _MONTHS[mon], int(d), tzinfo=timezone.utc), False
    m = _RE_ISO.search(title)
    if m:
        return datetime(*map(int, m.groups()), tzinfo=timezone.utc), False
    return datetime.fromtimestamp(ctime_ms / 1000, tz=timezone.utc), False


def fetch_kucoin_listings(page_size: int = 50) -> list[Listing]:
    data = _get_json("https://api.kucoin.com/api/v3/announcements",
                     annType="new-listings", lang="en_US", pageSize=page_size, currentPage=1)
    out = []
    for it in data["data"]["items"]:
        title, desc = it["annTitle"], it.get("annDesc", "")
        futures = title.startswith("KuCoin Futures")
        if futures:
            m = re.search(r"New Listing: (\w+?)(?:USDT)? ", title)
            symbols = [m.group(1)] if m and "Will Relist" not in title else []
        else:
            symbols = re.findall(r"\(([A-Z0-9]{2,12})\)", title) or re.findall(r"\b([A-Z0-9]{3,8})\b(?=[,)]| and )", desc)[:4]
        if not symbols:
            continue
        start, known = _parse_kucoin_start(title, desc, it["cTime"])
        for sym in symbols:
            out.append(Listing("KuCoin", "futures" if futures else "spot", sym, start, known, title, it.get("annUrl", "")))
    return out


def fetch_gate_listings() -> list[Listing]:
    """Paires spot dont l'ouverture du trading (buy_start) est dans le futur."""
    now = time.time()
    pairs = _get_json("https://api.gateio.ws/api/v4/spot/currency_pairs")
    return [
        Listing("Gate.io", "spot", p["base"], datetime.fromtimestamp(p["buy_start"], tz=timezone.utc), True,
                f"{p['id']} ({p.get('base_name', '')})", p.get("trade_url", ""))
        for p in pairs if p.get("buy_start", 0) > now and p["quote"] == "USDT"
    ]


def scan_listings(days_ahead: int = 1, days_back: int = 0) -> list[Listing]:
    """Listings dont la date tombe entre (aujourd'hui - days_back) et (aujourd'hui + days_ahead), UTC."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    lo, hi = today - timedelta(days=days_back), today + timedelta(days=days_ahead)
    found = []
    for fetch in (fetch_kucoin_listings, fetch_gate_listings):
        try:
            found += fetch()
        except Exception as e:  # noqa: BLE001 - une source en panne ne bloque pas les autres
            print(f"[warn] {fetch.__name__}: {type(e).__name__}: {e}")
    return sorted((l for l in found if lo <= l.start < hi), key=lambda l: l.start)


# ---------------------------------------------------------------------------
# Module 2 - Filtre de sécurité anti-scam
# ---------------------------------------------------------------------------
MAX_TAX = 0.10          # taxe achat/vente max tolérée
MIN_LIQUIDITY_USD = 25_000
MIN_LOCKED_LP = 0.80    # part de LP verrouillée/brûlée


@dataclass
class AuditResult:
    chain_id: str
    address: str
    passed: bool = False
    blockers: list = field(default_factory=list)   # rédhibitoires
    warnings: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def _flag(d: dict, key: str) -> bool:
    return d.get(key) == "1"


def _frac(d: dict, key: str):
    try:
        return float(d[key])
    except (KeyError, ValueError, TypeError):
        return None


DEXSCREENER_CHAIN = {"1": "ethereum", "56": "bsc", "137": "polygon", "42161": "arbitrum", "8453": "base",
                     "10": "optimism", "43114": "avalanche", "250": "fantom", "324": "zksync", "59144": "linea"}


def audit_token(chain_id: str, address: str, require_locked_lp: bool = True) -> AuditResult:
    """chain_id GoPlus : 1=ETH, 56=BSC, 137=Polygon, 42161=Arbitrum, 8453=Base..."""
    address = address.lower()
    res = AuditResult(chain_id, address)

    sec = _get_json(f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}", contract_addresses=address)
    d = (sec.get("result") or {}).get(address)
    if not d:
        res.blockers.append("Contrat inconnu de GoPlus (pas d'audit possible)")
        return res

    if _flag(d, "is_honeypot"):
        res.blockers.append("HONEYPOT détecté")
    if _flag(d, "cannot_sell_all"):
        res.blockers.append("Impossible de vendre tout son solde")
    if _flag(d, "cannot_buy"):
        res.blockers.append("Achat impossible")
    if _flag(d, "owner_change_balance"):
        res.blockers.append("Le owner peut modifier les soldes")
    if _flag(d, "selfdestruct"):
        res.blockers.append("Fonction selfdestruct")

    buy_tax, sell_tax = _frac(d, "buy_tax"), _frac(d, "sell_tax")
    res.metrics.update(buy_tax=buy_tax, sell_tax=sell_tax)
    for name, tax in (("achat", buy_tax), ("vente", sell_tax)):
        if tax is not None and tax > MAX_TAX:
            res.blockers.append(f"Taxe {name} {tax:.0%} > {MAX_TAX:.0%}")
    if buy_tax is None or sell_tax is None:
        res.warnings.append("Taxes non vérifiables (simulation impossible)")

    for key, msg in (("is_mintable", "Supply mintable"), ("hidden_owner", "Owner caché"),
                     ("can_take_back_ownership", "Ownership récupérable"),
                     ("transfer_pausable", "Transferts pausables"), ("is_blacklisted", "Blacklist possible"),
                     ("trading_cooldown", "Cooldown de trading"), ("is_proxy", "Contrat proxy (upgradable)"),
                     ("slippage_modifiable", "Taxes modifiables par le owner")):
        if _flag(d, key):
            res.warnings.append(msg)
    if d.get("is_open_source") == "0":
        res.warnings.append("Code non vérifié (non open source)")

    # LP verrouillée / brûlée
    locked = 0.0
    for h in d.get("lp_holders") or []:
        pct = float(h.get("percent") or 0)
        if h.get("is_locked") == 1 or h.get("address", "").lower() in (
                "0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000"):
            locked += pct
    res.metrics["lp_locked"] = locked
    if not d.get("lp_holders"):
        res.warnings.append("Aucune info sur la LP (pas de pool ou non listée)")
    elif locked < MIN_LOCKED_LP:
        msg = f"LP verrouillée/brûlée {locked:.0%} < {MIN_LOCKED_LP:.0%}"
        (res.blockers if require_locked_lp else res.warnings).append(msg)

    holder_pct = _frac(d, "creator_percent")
    if holder_pct is not None and holder_pct > 0.10:
        res.warnings.append(f"Créateur détient {holder_pct:.0%} de la supply")

    # Liquidité & prix (DexScreener) : meilleure pool par liquidité
    try:
        pairs = _get_json(f"https://api.dexscreener.com/latest/dex/tokens/{address}").get("pairs") or []
        chain_name = DEXSCREENER_CHAIN.get(str(chain_id))
        if chain_name:      # ne garder que les pools de la chaîne auditée (un même contrat existe sur plusieurs chaînes)
            pairs = [p for p in pairs if p.get("chainId") == chain_name]
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0), default=None)
        if best:
            liq = (best.get("liquidity") or {}).get("usd", 0)
            res.metrics.update(liquidity_usd=liq, price_usd=best.get("priceUsd"), dex=best.get("dexId"),
                               fdv=best.get("fdv"), pair_url=best.get("url"))
            if liq < MIN_LIQUIDITY_USD:
                res.blockers.append(f"Liquidité ${liq:,.0f} < ${MIN_LIQUIDITY_USD:,.0f}")
        else:
            res.warnings.append("Aucune pool DexScreener")
    except Exception as e:  # noqa: BLE001
        res.warnings.append(f"DexScreener indisponible ({type(e).__name__})")

    res.passed = not res.blockers
    return res


# ---------------------------------------------------------------------------
# Module 3 - Plan de trade listing
# ---------------------------------------------------------------------------
@dataclass
class ListingPlanConfig:
    initial_sl: float = 0.25      # SL initial : -25 % sous l'entrée
    tp1: float = 1.00             # +100 %
    tp1_fraction: float = 0.50    # vendre 50 % => capital récupéré
    tp2: float = 3.00             # +300 %
    tp2_fraction: float = 0.25    # puis 25 % de la position initiale
    trail_pct: float = 0.15       # trailing serré dès que le prix monte
    trail_after_tp1: float = 0.12
    trail_after_tp2: float = 0.08


@dataclass
class Action:
    kind: str          # TP1 | TP2 | STOP | TRAILING_STOP
    price: float       # prix d'exécution simulé (tick - slippage)
    fraction: float    # part de la position INITIALE à vendre
    note: str = ""
    tick_price: float = 0.0   # prix du tick (dernier trade / milieu) qui a DÉCLENCHÉ l'ordre
    bid: float = 0.0          # Bid utilisé pour l'exécution (0 = pas de carnet : prix du tick)


class ListingPosition:
    """Machine à états : nourrir `on_price()` avec chaque prix, elle renvoie les ordres de sortie."""

    def __init__(self, trigger_price: float, size_usd: float, cfg: ListingPlanConfig = ListingPlanConfig(),
                 slippage_penalty_pct: float = 5.0, ask: float | None = None):
        """Exécution réaliste sur le carnet :
          - ACHAT  : au meilleur Ask * (1 + pénalité)   (`ask`; à défaut le prix du tick)
          - VENTE  : au meilleur Bid * (1 - pénalité)   (voir `on_price(price, bid)`)
        Le prix du tick (dernier trade ou milieu bid/ask) ne sert que de DÉCLENCHEUR (TP/SL/trailing).
        Tous les seuils sont calculés sur le prix d'achat réellement obtenu (`self.entry`)."""
        self.cfg, self.size_usd = cfg, size_usd
        self.slippage = slippage_penalty_pct / 100
        self.tick_entry = trigger_price
        self.entry_ask = ask or trigger_price
        self.entry = self.entry_ask * (1 + self.slippage)     # prix d'achat réellement obtenu
        self.remaining = 1.0            # fraction de la position initiale encore ouverte
        self.peak = self.entry
        self.tp1_hit = self.tp2_hit = False
        self.stop = self.entry * (1 - cfg.initial_sl)

    def exit_price(self, trigger_price: float, bid: float | None = None) -> float:
        """Prix de vente simulé : Bid * (1 - pénalité) ; sans carnet, prix du tick * (1 - pénalité)."""
        return (bid or trigger_price) * (1 - self.slippage)

    @property
    def closed(self) -> bool:
        return self.remaining <= 1e-9

    def _trail(self) -> float:
        return self.cfg.trail_after_tp2 if self.tp2_hit else self.cfg.trail_after_tp1 if self.tp1_hit else self.cfg.trail_pct

    def on_price(self, price: float, bid: float | None = None) -> list[Action]:
        """`price` = déclencheur (dernier trade / milieu) ; `bid` = meilleur acheteur courant pour l'exécution."""
        if self.closed:
            return []
        acts = []
        if price <= self.stop:      # le stop prime sur les TP si le prix gappe
            kind = "STOP" if self.stop <= self.entry * (1 - self.cfg.initial_sl) + 1e-12 else "TRAILING_STOP"
            acts.append(Action(kind, self.exit_price(price, bid), self.remaining, f"stop à {self.stop:.6g}", price, bid or 0.0))
            self.remaining = 0.0
            return acts

        if price > self.peak:
            self.peak = price
            if price > self.entry:  # trailing actif dès que le prix monte
                self.stop = max(self.stop, self.peak * (1 - self._trail()))

        if not self.tp1_hit and price >= self.entry * (1 + self.cfg.tp1):
            self.tp1_hit = True
            self.remaining -= self.cfg.tp1_fraction
            self.stop = max(self.stop, self.entry, self.peak * (1 - self._trail()))  # capital sécurisé
            acts.append(Action("TP1", self.exit_price(price, bid), self.cfg.tp1_fraction, "capital récupéré, stop >= entrée", price, bid or 0.0))
        if self.tp1_hit and not self.tp2_hit and price >= self.entry * (1 + self.cfg.tp2):
            self.tp2_hit = True
            frac = min(self.cfg.tp2_fraction, self.remaining)
            self.remaining -= frac
            self.stop = max(self.stop, self.peak * (1 - self._trail()))
            acts.append(Action("TP2", self.exit_price(price, bid), frac, "trailing resserré sur le runner", price, bid or 0.0))
        return acts


def describe_plan(entry: float, cfg: ListingPlanConfig = ListingPlanConfig()) -> str:
    return (f"Entrée {entry:.6g} | SL {entry * (1 - cfg.initial_sl):.6g} (-{cfg.initial_sl:.0%}) | "
            f"TP1 {entry * (1 + cfg.tp1):.6g} (+{cfg.tp1:.0%}, vend {cfg.tp1_fraction:.0%}) | "
            f"TP2 {entry * (1 + cfg.tp2):.6g} (+{cfg.tp2:.0%}, vend {cfg.tp2_fraction:.0%}) | "
            f"Trailing {cfg.trail_pct:.0%} -> {cfg.trail_after_tp1:.0%} -> {cfg.trail_after_tp2:.0%}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_scan(args) -> None:
    now = datetime.now(timezone.utc)
    print(f"Scan des listings - {now:%Y-%m-%d %H:%M} UTC "
          f"(fenêtre : J-{args.recent} -> J+{args.days})\n")
    listings = scan_listings(args.days, args.recent)
    today_l = [l for l in listings if l.start.date() == now.date()]
    if not today_l:
        print(f"Aucun listing prévu aujourd'hui ({now:%Y-%m-%d}) sur KuCoin / Gate.io.")
    for title, group in (("AUJOURD'HUI", today_l),
                         ("À VENIR", [l for l in listings if l.start.date() > now.date()]),
                         ("RÉCENTS", [l for l in listings if l.start.date() < now.date()])):
        if not group:
            continue
        print(f"== {title} ==")
        for l in group:
            when = f"{l.start:%Y-%m-%d %H:%M}" if l.time_known else f"{l.start:%Y-%m-%d} (heure ?)"
            delta = ""
            if l.time_known and l.start > now:
                h, m = divmod(int((l.start - now).total_seconds()) // 60, 60)
                delta = f"  dans {h}h{m:02d}"
            print(f"  [{l.exchange:<7}] {l.market:<7} {l.symbol:<10} {when} UTC{delta}")
        print()
    if args.notify:
        from telegram_notifier import notify_new_listings, telegram_configured
        if not args.dry_run and not telegram_configured():
            sys.exit("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID manquants (ou utilisez --dry-run).")
        upcoming = [l for l in listings if l.start >= now - timedelta(days=0, hours=1)]
        sent = notify_new_listings(upcoming, dry_run=args.dry_run)
        print(f"{len(sent)} alerte(s) Telegram {'simulée(s)' if args.dry_run else 'envoyée(s)'}.")


def cmd_watch(args) -> None:
    """Rescan périodique ; alerte Telegram à chaque nouveau listing (dédoublonné)."""
    from telegram_notifier import notify_new_listings, telegram_configured
    if not args.dry_run and not telegram_configured():
        sys.exit("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID manquants (ou utilisez --dry-run).")
    print(f"Surveillance des listings toutes les {args.interval}s (Ctrl+C pour arrêter)")
    while True:
        sent = notify_new_listings(scan_listings(args.days, 0), dry_run=args.dry_run)
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {len(sent)} nouvelle(s) alerte(s)")
        time.sleep(args.interval)


def _pnl(pos: "ListingPosition", realized: float, tick_price: float, bid: float | None = None) -> float:
    """PnL en % de la mise : ventes déjà faites + reste valorisé au Bid simulé (slippage inclus)."""
    return (realized + pos.remaining * pos.exit_price(tick_price, bid) / pos.entry - 1) * 100


def print_audit(r: "AuditResult") -> None:
    print(f"Audit {r.address} (chain {r.chain_id}) -> {'✅ OK' if r.passed else '⛔ REJETÉ'}")
    for b in r.blockers:
        print(f"  ⛔ {b}")
    for w in r.warnings:
        print(f"  ⚠️  {w}")
    print(f"  métriques: {r.metrics}")


def audit_gate(contract: str, chain_id: str, require_locked_lp: bool = True) -> "AuditResult":
    """Audit bloquant (fail-closed) : toute erreur d'API est traitée comme un échec."""
    try:
        return audit_token(chain_id, contract, require_locked_lp)
    except Exception as e:  # noqa: BLE001
        r = AuditResult(chain_id, contract.lower())
        r.blockers.append(f"Audit impossible ({type(e).__name__}: {e}) — refus par prudence")
        return r


def cmd_monitor(args) -> None:
    """Flux de prix temps réel -> ListingPosition ; exécution SIMULÉE (avec slippage) affichée en direct.

    Gate.io / KuCoin en mode auto|ws : « WS-first » — on s'abonne au WebSocket AVANT la cotation et le
    premier tick reçu déclenche l'entrée (aucun polling REST). OKX / mode rest : sonde REST puis flux.
    """
    from price_feed import FeedStats, TickRecorder, WS_EXCHANGES, stream_prices, stream_replay, wait_until_listed
    from telegram_notifier import notify_text

    ex, sym = args.exchange.lower(), args.symbol.upper()
    replay = ex == "replay"
    out = (lambda m: notify_text(m, dry_run=args.dry_run)) if args.notify else (lambda m: None)

    if args.contract:                      # filtre de sécurité AVANT toute attente / entrée
        print(f"🔎 Audit de sécurité du contrat {args.contract} (chaîne {args.chain}) avant d'autoriser l'entrée…")
        res = audit_gate(args.contract, args.chain, not args.allow_unlocked_lp)
        print_audit(res)
        if not res.passed:
            msg = f"⛔ Audit ÉCHOUÉ pour {sym} : trade simulé REFUSÉ, attente annulée. " + "; ".join(res.blockers)
            print(msg)
            out(f"<b>{msg}</b>")
            sys.exit(3)
        print("✅ Audit validé : entrée autorisée.")
    ts = lambda: f"{datetime.now(timezone.utc):%H:%M:%S.%f}"[:-3]
    ws_first = ex in WS_EXCHANGES and args.mode in ("auto", "ws")

    recorder = TickRecorder(args.record, {"exchange": ex, "symbol": sym, "started": time.time()}) if args.record else None
    stats = FeedStats()
    if replay:
        def _counted(src):
            for t in src:
                stats.record(t)
                yield t
        feed = _counted(stream_replay(args.symbol, args.speed))   # `symbol` = chemin du fichier
        stats.mode = f"replay x{args.speed:g}"
        sym = Path(args.symbol).stem.upper()
    elif ex == "mock":
        feed = stream_prices(ex, sym, args.mode, args.interval, stats)
    elif ws_first:
        print(f"[{ts()}] WS-first : abonnement {ex} {sym}/USDT AVANT la cotation, 1er tick => entrée (pas de polling REST)")
        feed = stream_prices(ex, sym, args.mode, args.interval, stats)
    else:
        print(f"[{ts()}] Attente de la cotation {sym} sur {ex} (sonde REST {args.poll}s)…")
        if wait_until_listed(ex, sym, args.poll, args.wait_timeout) is None:
            sys.exit("Timeout : le jeton n'est toujours pas coté.")
        feed = stream_prices(ex, sym, args.mode, args.interval, stats)

    pos, realized, last_status, t_start = None, 0.0, 0.0, time.time()
    quote = None                       # (bid, ask) courant du carnet
    last_price, skipped_no_book, seen_reconnects, closed_at = None, 0, 0, 0.0
    cfg = ListingPlanConfig()
    try:
        for tick in feed:
            elapsed = time.time() - t_start
            if tick is None:                       # battement : pas de tick depuis ~1 s
                if pos is None and args.wait_timeout and elapsed > args.wait_timeout:
                    print(f"[{ts()}] Timeout : aucun tick reçu en {args.wait_timeout:g}s")
                    break
                if args.duration and elapsed > args.duration:
                    print(f"[{ts()}] Durée max atteinte ({args.duration:g}s)")
                    break
                if pos is not None and pos.closed and time.time() - closed_at > args.record_after_close:
                    break                          # fenêtre d'enregistrement post-clôture écoulée (marché muet)
                continue
            if recorder:
                recorder.write(tick)
            last_price = tick.price

            # --- Carnet : Bid/Ask d'exécution -------------------------------------------------
            if stats.reconnects != seen_reconnects:    # après une reconnexion, l'ancien carnet n'est plus fiable
                seen_reconnects, quote = stats.reconnects, None
            if tick.bid and tick.ask:
                quote = (tick.bid, tick.ask)           # tick "book" : carnet à jour
            elif tick.kind == "price":
                quote = (tick.price, tick.price)       # source sans carnet (mock/REST/anciens fichiers) : prix = bid = ask
            # tick "trade" sans bid/ask : on garde le dernier carnet connu ; il ne sert que de déclencheur.
            bid = quote[0] if quote else None

            if pos is None:
                if quote is None:                      # trade avant tout carnet : pas d'Ask => on n'achète pas à l'aveugle
                    skipped_no_book += 1
                    continue
                pos = ListingPosition(tick.price, args.size, cfg, args.slippage, ask=quote[1])
                lat = f" | délai exchange->réception {(tick.recv_ts - tick.exch_ts) * 1000:.0f} ms" if tick.exch_ts else ""
                print(f"[{ts()}] 🟢 ENTRÉE simulée {sym} — déclencheur {tick.price:.8g} ({tick.kind}), Ask {quote[1]:.8g} "
                      f"→ exécutée {pos.entry:.8g} (+{args.slippage:g}% slippage), mise {args.size:g}$, feed {stats.mode}{lat}")
                print("          " + describe_plan(pos.entry, cfg))
                out(f"<b>🟢 ENTRÉE simulée {sym}</b> ({ex}) Ask {quote[1]:.8g} → {pos.entry:.8g}\n{describe_plan(pos.entry, cfg)}")
                continue
            if pos.closed:                             # position fermée : on ne fait plus qu'enregistrer
                if (args.duration and elapsed > args.duration) or time.time() - closed_at > args.record_after_close:
                    break
                continue
            for a in pos.on_price(tick.price, bid):
                realized += a.fraction * a.price / pos.entry
                icon = {"TP1": "🎯", "TP2": "🚀", "STOP": "🛑", "TRAILING_STOP": "🔒"}[a.kind]
                src = f"Bid {a.bid:.8g}" if a.bid else "prix tick"
                line = (f"{icon} {a.kind} déclencheur {a.tick_price:.8g} → vendu {a.price:.8g} ({src} -{args.slippage:g}%, "
                        f"{a.price / pos.entry:.2f}x) — vend {a.fraction:.0%} — {a.note} — reste {pos.remaining:.0%}")
                print(f"[{ts()}] {line}")
                out(f"<b>{sym}</b> {line}")
            if pos.closed:
                closed_at = time.time()
                if not (recorder and args.record_after_close > 0):
                    break
                print(f"[{ts()}] Position clôturée — enregistrement poursuivi {args.record_after_close:g}s")
                continue
            if time.time() - last_status >= args.status_every and not pos.closed:
                last_status = time.time()
                print(f"[{ts()}] · {tick.price:.8g} ({tick.price / pos.entry:.2f}x fill) bid {bid:.8g} pic {pos.peak / pos.entry:.2f}x "
                      f"stop {pos.stop / pos.entry:.2f}x reste {pos.remaining:.0%} PnL {_pnl(pos, realized, tick.price, bid):+.1f}%")
            if args.duration and elapsed > args.duration:
                print(f"[{ts()}] Durée max atteinte ({args.duration:g}s)")
                break
    except KeyboardInterrupt:
        print("\nArrêt manuel.")
    except RuntimeError as e:
        print(f"[feed] {e}")
    finally:
        if recorder:
            recorder.close()
            print(f"Enregistrement: {recorder.n} ticks -> {args.record}")

    print("\n=== Résumé de la simulation ===")
    if pos:
        final = (realized - 1) * 100 if pos.closed else _pnl(pos, realized, last_price, quote[0] if quote else None)
        print(f"Slippage {args.slippage:g}% | tick d'entrée {pos.tick_entry:.8g} → fill {pos.entry:.8g} | "
              f"plus haut {pos.peak / pos.entry:.2f}x | TP1 {'oui' if pos.tp1_hit else 'non'} | "
              f"TP2 {'oui' if pos.tp2_hit else 'non'} | "
              f"{'clôturée' if pos.closed else f'ouverte ({pos.remaining:.0%}, valorisée à la sortie simulée)'} | "
              f"PnL simulé {final:+.1f}% ({args.size * final / 100:+.2f}$ sur {args.size:g}$)")
        out(f"<b>Résumé {sym}</b> : PnL simulé {final:+.1f}% (plus haut {pos.peak / pos.entry:.2f}x, slippage {args.slippage:g}%)")
    else:
        print("Aucun tick reçu.")
    lat = f"{sum(stats.lat_ms) / len(stats.lat_ms):.0f} ms moy." if stats.lat_ms else "n/a"
    ready = f" | WS prêt (connexion+abonnement) {stats.ws_ready_ms:.0f} ms" if stats.ws_ready_ms else ""
    first = f" | 1er tick après {stats.first_tick_ts - t_start:.2f}s" if stats.first_tick_ts and not replay else ""
    if skipped_no_book:
        print(f"Ticks 'trade' ignorés avant le 1er carnet (pas d'Ask pour acheter) : {skipped_no_book}")
    print(f"Feed: {stats.mode} | {stats.ticks} ticks | reconnexions {stats.reconnects} | "
          f"plus grand silence {stats.max_gap:.2f}s | latence reçue-vs-exchange {lat}{ready}{first}")


SUPPORTED_WS = {"KuCoin": "kucoin", "Gate.io": "gate"}


def _inject_listing(spec: str) -> "Listing":
    """Test : --inject EXCHANGE:SYMBOLE:+SECONDES  (ex. gate:FAKE:+20) ajoute un listing fictif pour aujourd'hui."""
    exch, symbol, delay = spec.split(":")
    names = {v: k for k, v in SUPPORTED_WS.items()} | {"mock": "Mock"}
    return Listing(names.get(exch, exch), "spot", symbol.upper(),
                   datetime.now(timezone.utc) + timedelta(seconds=float(delay)), True, f"[INJECTÉ] {symbol}", "")


def cmd_hunter(args) -> None:
    """Chasseur automatique : scanne les annonces en boucle et lance (sous-processus) `monitor --record`
    pour chaque listing SPOT du jour, `--lead` secondes avant l'heure annoncée (ou tout de suite si l'heure est inconnue)."""
    import json
    import subprocess

    from telegram_notifier import notify_new_listings, telegram_configured
    if args.notify and not args.dry_run and not telegram_configured():
        sys.exit("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID manquants (ou utilisez --dry-run).")
    outdir = Path(args.record_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    state_path = outdir / ".hunter_started.json"
    try:
        started = set(json.loads(state_path.read_text()))
    except (OSError, ValueError):
        started = set()
    procs: list = []
    injected = [_inject_listing(x) for x in args.inject]
    print(f"🏹 Hunter actif — scan toutes les {args.interval}s, abonnement {args.lead}s avant le lancement, "
          f"ticks -> {outdir}/ (Ctrl+C pour arrêter)")

    def spawn(l: "Listing", ex_id: str) -> None:
        now = datetime.now(timezone.utc)
        until = max(0.0, (l.start - now).total_seconds()) if l.time_known else 0.0
        tag = f"{ex_id}_{l.symbol}_{l.start:%Y%m%d_%H%M}"
        record, logf = outdir / f"{tag}.json", outdir / f"{tag}.log"
        # attente max : jusqu'à l'heure annoncée + 10 min de retard toléré (6 h si l'heure est inconnue)
        wait = until + 600 if l.time_known else 6 * 3600
        cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "monitor", ex_id, l.symbol,
               "--record", str(record), "--record-after-close", f"{args.post * 60:.0f}", "--wait-timeout", f"{wait:.0f}",
               "--duration", f"{wait + args.post * 60:.0f}", "--size", str(args.size), "--slippage", str(args.slippage)]
        if args.notify:
            cmd.append("--notify")
        if args.dry_run:
            cmd.append("--dry-run")
        procs.append((tag, subprocess.Popen(cmd, stdout=open(logf, "w"), stderr=subprocess.STDOUT)))
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}] ▶ monitor lancé : {ex_id} {l.symbol} "
              f"({'dans ' + str(int(until)) + 's' if l.time_known else 'heure inconnue'}) -> {record.name}")

    try:
        while True:
            now = datetime.now(timezone.utc)
            listings = scan_listings(1, 0) + injected
            if args.notify:
                notify_new_listings([l for l in listings if l.start >= now - timedelta(hours=1)], dry_run=args.dry_run)
            for l in listings:
                ex_id = SUPPORTED_WS.get(l.exchange, "mock" if l.exchange == "Mock" else None)
                key = f"{l.exchange}|{l.market}|{l.symbol}|{l.start:%Y-%m-%dT%H:%M}"
                if l.start.date() != now.date() or key in started:
                    continue
                if not ex_id or l.market != "spot":
                    continue                                      # futures / exchange sans flux : ignorés
                if l.time_known and l.start < now - timedelta(seconds=60):
                    continue                                      # lancement déjà passé : rien à capturer
                if l.time_known and (l.start - now).total_seconds() > args.lead:
                    continue                                      # trop tôt : on attend la fenêtre d'abonnement
                started.add(key)
                state_path.write_text(json.dumps(sorted(started)))
                spawn(l, ex_id)
            for tag, pr in list(procs):
                if pr.poll() is not None:
                    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] ■ monitor terminé : {tag} (code {pr.returncode})")
                    procs.remove((tag, pr))
            if args.once and not procs and (now - args.t0).total_seconds() > args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nArrêt du hunter ; les monitors en cours continuent jusqu'à leur durée max." if procs else "\nArrêt.")


def cmd_preflight(args) -> None:
    """Vérifie l'environnement du serveur avant le déploiement du hunter (réseau, WebSockets, horloge, disque)."""
    import json as _json
    import tempfile

    import websocket

    from price_feed import FeedStats, _kucoin_info, _sslopt, _ws_gate, _ws_kucoin
    results = []

    def check(name, fn, critical=True):
        t = time.time()
        try:
            detail = fn()
            results.append((name, True, f"{(time.time() - t) * 1000:.0f} ms" + (f" — {detail}" if detail else ""), critical))
        except Exception as e:  # noqa: BLE001
            results.append((name, False, f"{type(e).__name__}: {str(e)[:90]}", critical))

    def ws_ack(gen_fn):
        st, it = FeedStats(), None
        it = gen_fn("BTC", st)
        deadline = time.time() + 10
        for item in it:                        # 1er élément = connexion + abonnement établis (tick ou battement)
            return f"WS prêt en {st.ws_ready_ms:.0f} ms"
            if time.time() > deadline:
                raise TimeoutError

    def skew():
        t0 = time.time()
        srv = _get_json("https://api.gateio.ws/api/v4/spot/time")["server_time"] / 1000
        t1 = time.time()
        d = (t0 + t1) / 2 - srv
        if abs(d) > 1.0:
            raise RuntimeError(f"horloge locale décalée de {d:+.2f}s (installez/activez NTP)")
        return f"décalage horloge {d * 1000:+.0f} ms"

    def disk():
        Path(args.record_dir).mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=args.record_dir):
            pass
        return f"{args.record_dir}/ inscriptible"

    check("KuCoin annonces (REST)", lambda: f"{len(fetch_kucoin_listings())} annonces analysées")
    check("Gate.io paires (REST)", lambda: f"{len(fetch_gate_listings())} listing(s) futur(s)")
    check("Gate.io WebSocket", lambda: ws_ack(_ws_gate))
    check("KuCoin WebSocket", lambda: ws_ack(_ws_kucoin))
    check("GoPlus (audit)", lambda: _get_json("https://api.gopluslabs.io/api/v1/token_security/1",
                                              contract_addresses="0xdac17f958d2ee523a2206206994597c13d831ec7") and "")
    check("DexScreener (audit)", lambda: _get_json("https://api.dexscreener.com/latest/dex/tokens/0xdac17f958d2ee523a2206206994597c13d831ec7") and "")
    check("Horloge vs Gate.io", skew)
    check("Disque d'enregistrement", disk)
    from telegram_notifier import telegram_configured
    results.append(("Telegram (variables d'env.)", telegram_configured(),
                    "TELEGRAM_TOKEN + TELEGRAM_CHAT_ID présents" if telegram_configured() else "absentes → pas d'alertes", False))
    print("=== Préflight hunter ===")
    for name, ok, detail, crit in results:
        print(f"  {'✅' if ok else ('⛔' if crit else '⚠️ ')} {name:<28} {detail}")
    if not all(ok for _, ok, _, crit in results if crit):
        sys.exit("Préflight ÉCHOUÉ : corrigez les points ⛔ avant de lancer le hunter.")
    print("Préflight OK (les points ⚠️ sont facultatifs).")


def cmd_audit(args) -> None:
    print_audit(audit_gate(args.address, args.chain_id, not args.allow_unlocked_lp))


def cmd_simulate(_args) -> None:
    entry = 1.0
    print(describe_plan(entry), "\n")
    pos = ListingPosition(entry, size_usd=100)
    path = [1.0, 1.3, 1.8, 2.1, 2.5, 3.2, 4.2, 5.5, 5.0, 4.6, 4.9]   # pump puis repli
    for p in path:
        for a in pos.on_price(p):
            print(f"prix {p:>4}x -> {a.kind:<14} vend {a.fraction:.0%} de la position  ({a.note})")
        if pos.closed:
            break
    print(f"\nRestant: {pos.remaining:.0%} | stop courant: {pos.stop:.2f}x | plus haut: {pos.peak:.2f}x")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("scan")
    s.add_argument("--days", type=int, default=1, help="jours à venir (défaut 1 = aujourd'hui)")
    s.add_argument("--recent", type=int, default=0, help="inclure les listings des N derniers jours")
    s.add_argument("--notify", action="store_true", help="alerte Telegram pour les listings non encore notifiés")
    s.add_argument("--dry-run", action="store_true", help="affiche les messages au lieu de les envoyer")
    w = sub.add_parser("watch")
    w.add_argument("--interval", type=int, default=300)
    w.add_argument("--days", type=int, default=1)
    w.add_argument("--dry-run", action="store_true")
    h = sub.add_parser("hunter", help="scanne les annonces et lance monitor --record automatiquement")
    h.add_argument("--interval", type=int, default=60, help="période du scan d'annonces (s)")
    h.add_argument("--lead", type=int, default=300, help="s'abonner N s avant l'heure de lancement annoncée")
    h.add_argument("--post", type=float, default=30, help="minutes d'enregistrement après la clôture de la position")
    h.add_argument("--record-dir", default="ticks")
    h.add_argument("--size", type=float, default=100.0)
    h.add_argument("--slippage", type=float, default=5.0)
    h.add_argument("--notify", action="store_true", help="alertes Telegram (listing détecté + événements du monitor)")
    h.add_argument("--dry-run", action="store_true")
    h.add_argument("--inject", action="append", default=[], metavar="EXCHANGE:SYMBOLE:+SEC",
                   help="TEST : ajoute un listing fictif du jour (ex. gate:FAKE:+20)")
    h.add_argument("--once", type=int, default=0, metavar="SEC",
                   help="TEST : quitte quand plus aucun monitor n'est actif (après au moins SEC secondes)")
    a = sub.add_parser("audit")
    a.add_argument("chain_id")
    a.add_argument("address")
    a.add_argument("--allow-unlocked-lp", action="store_true", help="LP non verrouillée = avertissement (pools V3 en NFT)")
    m = sub.add_parser("monitor", help="flux de prix temps réel + exécution simulée du plan")
    m.add_argument("exchange", help="gate | kucoin | okx | mock (flux simulé) | replay (rejeu d'un fichier)")
    m.add_argument("symbol", help="symbole, ou chemin du fichier si exchange=replay")
    m.add_argument("--mode", choices=["auto", "ws", "rest"], default="auto")
    m.add_argument("--interval", type=float, default=0.5, help="période REST (s) / vitesse du mock")
    m.add_argument("--poll", type=float, default=0.5, help="période de sonde avant la cotation (s)")
    m.add_argument("--wait-timeout", type=float, default=None, help="abandonner l'attente de cotation après N s")
    m.add_argument("--size", type=float, default=100.0, help="mise simulée en $")
    m.add_argument("--duration", type=float, default=0, help="durée max en s (0 = illimité)")
    m.add_argument("--status-every", type=float, default=5.0)
    m.add_argument("--notify", action="store_true", help="envoie aussi les événements sur Telegram")
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--slippage", type=float, default=5.0, help="pénalité de slippage en %% (achat +x%%, vente -x%%)")
    m.add_argument("--record", metavar="FICHIER.json", help="enregistre chaque tick (ts ms, prix, volume…)")
    m.add_argument("--contract", help="adresse du contrat (DEX) : audit GoPlus/DexScreener obligatoire avant l'entrée")
    m.add_argument("--chain", default="1", help="chain_id GoPlus du contrat (1=ETH, 56=BSC, 8453=Base…)")
    m.add_argument("--allow-unlocked-lp", action="store_true", help="LP non verrouillée = avertissement (pools V3 en NFT)")
    m.add_argument("--record-after-close", type=float, default=0.0, metavar="SEC",
                   help="avec --record : continue d'enregistrer SEC secondes après la clôture de la position")
    m.add_argument("--speed", type=float, default=1.0, help="vitesse de rejeu (exchange=replay ; 0 = instantané)")
    sub.add_parser("simulate")
    pf = sub.add_parser("preflight", help="vérifie réseau, WebSockets, horloge et disque avant déploiement")
    pf.add_argument("--record-dir", default="ticks")
    t = sub.add_parser("analyze_ticks", aliases=["analyze-ticks"], help="statistiques de liquidité d'un enregistrement (sans machine à états)")
    t.add_argument("file")
    t.add_argument("--json", action="store_true")
    args = p.parse_args()
    if args.cmd == "analyze-ticks":
        args.cmd = "analyze_ticks"
    if args.cmd == "hunter":
        args.t0 = datetime.now(timezone.utc)
    handlers = {"audit": cmd_audit, "simulate": cmd_simulate, "monitor": cmd_monitor, "watch": cmd_watch, "hunter": cmd_hunter, "preflight": cmd_preflight,
                "analyze_ticks": lambda a: __import__("tick_analysis").run(a.file, a.json)}
    if args.cmd in handlers:
        handlers[args.cmd](args)
    else:
        cmd_scan(args if args.cmd else argparse.Namespace(days=1, recent=0, notify=False, dry_run=False))


if __name__ == "__main__":
    main()
