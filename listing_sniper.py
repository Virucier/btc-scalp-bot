"""Listing Scanner & Safety Filter.

Module 1 - détection des listings à venir (KuCoin annonces, Gate.io paires futures)
Module 2 - audit anti-scam d'un contrat (GoPlus Security + DexScreener)
Module 3 - plan de trade listing (TP1 +100 %, TP2 +300 %, trailing stop serré)

Usage:
    python listing_sniper.py scan [--days 1] [--recent 7]
    python listing_sniper.py audit <chain_id> <adresse_contrat>
    python listing_sniper.py simulate

Outil d'analyse uniquement : aucun ordre n'est passé. Les listings sont extrêmement
volatils et le risque de perte totale est réel.
"""
import argparse
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

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


def audit_token(chain_id: str, address: str) -> AuditResult:
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
        res.blockers.append(f"LP verrouillée/brûlée {locked:.0%} < {MIN_LOCKED_LP:.0%}")

    holder_pct = _frac(d, "creator_percent")
    if holder_pct is not None and holder_pct > 0.10:
        res.warnings.append(f"Créateur détient {holder_pct:.0%} de la supply")

    # Liquidité & prix (DexScreener) : meilleure pool par liquidité
    try:
        pairs = _get_json(f"https://api.dexscreener.com/latest/dex/tokens/{address}").get("pairs") or []
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
    price: float
    fraction: float    # part de la position INITIALE à vendre
    note: str = ""


class ListingPosition:
    """Machine à états : nourrir `on_price()` avec chaque prix, elle renvoie les ordres de sortie."""

    def __init__(self, entry: float, size_usd: float, cfg: ListingPlanConfig = ListingPlanConfig()):
        self.cfg, self.entry, self.size_usd = cfg, entry, size_usd
        self.remaining = 1.0            # fraction de la position initiale encore ouverte
        self.peak = entry
        self.tp1_hit = self.tp2_hit = False
        self.stop = entry * (1 - cfg.initial_sl)

    @property
    def closed(self) -> bool:
        return self.remaining <= 1e-9

    def _trail(self) -> float:
        return self.cfg.trail_after_tp2 if self.tp2_hit else self.cfg.trail_after_tp1 if self.tp1_hit else self.cfg.trail_pct

    def on_price(self, price: float) -> list[Action]:
        if self.closed:
            return []
        acts = []
        if price <= self.stop:      # le stop prime sur les TP si le prix gappe
            kind = "STOP" if self.stop <= self.entry * (1 - self.cfg.initial_sl) + 1e-12 else "TRAILING_STOP"
            acts.append(Action(kind, price, self.remaining, f"stop à {self.stop:.6g}"))
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
            acts.append(Action("TP1", price, self.cfg.tp1_fraction, "capital récupéré, stop >= entrée"))
        if self.tp1_hit and not self.tp2_hit and price >= self.entry * (1 + self.cfg.tp2):
            self.tp2_hit = True
            frac = min(self.cfg.tp2_fraction, self.remaining)
            self.remaining -= frac
            self.stop = max(self.stop, self.peak * (1 - self._trail()))
            acts.append(Action("TP2", price, frac, "trailing resserré sur le runner"))
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


def cmd_audit(args) -> None:
    r = audit_token(args.chain_id, args.address)
    print(f"Audit {r.address} (chain {r.chain_id}) -> {'✅ OK' if r.passed else '⛔ REJETÉ'}")
    for b in r.blockers:
        print(f"  ⛔ {b}")
    for w in r.warnings:
        print(f"  ⚠️  {w}")
    print(f"  métriques: {r.metrics}")


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
    a = sub.add_parser("audit")
    a.add_argument("chain_id")
    a.add_argument("address")
    sub.add_parser("simulate")
    args = p.parse_args()
    {"audit": cmd_audit, "simulate": cmd_simulate}.get(args.cmd, cmd_scan)(
        args if args.cmd else argparse.Namespace(days=1, recent=0))


if __name__ == "__main__":
    main()
