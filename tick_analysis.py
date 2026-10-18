"""« Physique du listing » : statistiques de liquidité extraites d'un enregistrement de ticks
(`monitor --record`), sans exécuter la machine à états.

Référence temporelle t0 = premier tick enregistré (pour une capture hunter : l'ouverture du marché)."""
import json
import statistics
from typing import Optional

from price_feed import load_ticks

W_SPREAD, W_RISE, W_VOLUME = 10.0, 1.0, 60.0     # fenêtres (s) : spread, hausse, volume


def _pct(a: float, b: float) -> Optional[float]:
    return (a / b - 1) * 100 if b else None


def analyze(path: str) -> dict:
    rows = sorted(load_ticks(path), key=lambda r: r["ts_ms"])
    if not rows:
        raise ValueError("aucun tick exploitable dans le fichier")
    t0 = rows[0]["ts_ms"]
    off = lambda r: (r["ts_ms"] - t0) / 1000
    duration = off(rows[-1])
    p0 = rows[0]["price"]
    kinds = {}
    for r in rows:
        kinds[r.get("kind") or "price"] = kinds.get(r.get("kind") or "price", 0) + 1
    out = {"file": path, "ticks": len(rows), "duration_s": round(duration, 2), "kinds": kinds, "first_price": p0}

    # --- Spread (10 premières secondes) ------------------------------------------------------
    books = [r for r in rows if r.get("bid") and r.get("ask") and r["ask"] >= r["bid"]]
    early = [r for r in books if off(r) <= W_SPREAD]
    sp = lambda r: (r["ask"] - r["bid"]) / ((r["ask"] + r["bid"]) / 2) * 100
    if early:
        worst = max(early, key=sp)
        pcts = [sp(r) for r in early]
        out["spread_10s"] = {
            "book_ticks": len(early), "max_pct": round(sp(worst), 4), "max_abs": worst["ask"] - worst["bid"],
            "max_at_s": round(off(worst), 2), "median_pct": round(statistics.median(pcts), 4),
            "first_book_pct": round(sp(early[0]), 4), "first_book_at_s": round(off(early[0]), 2)}
    else:
        out["spread_10s"] = None
    if books:
        out["spread_overall"] = {"median_pct": round(statistics.median(sp(r) for r in books), 4),
                                 "max_pct": round(max(sp(r) for r in books), 4)}

    # --- Hausse (1ère seconde) ---------------------------------------------------------------
    first = [r for r in rows if off(r) <= W_RISE]
    peak = max(first, key=lambda r: r["price"])
    low = min(first, key=lambda r: r["price"])
    out["first_second"] = {
        "ticks": len(first), "peak_rise_pct": round(_pct(peak["price"], p0), 3), "peak_at_s": round(off(peak), 3),
        "rise_at_1s_pct": round(_pct(first[-1]["price"], p0), 3), "max_drawdown_pct": round(_pct(low["price"], p0), 3)}
    for label, w in (("+10s", 10), ("+60s", 60)):
        w_rows = [r for r in rows if off(r) <= w]
        if duration >= w:
            out.setdefault("price_path", {})[label] = round(_pct(w_rows[-1]["price"], p0), 3)

    # --- Volume (1ère minute) ----------------------------------------------------------------
    trades = [r for r in rows if (r.get("kind") == "trade") and (r.get("volume") or 0) > 0 and off(r) <= W_VOLUME]
    out["volume_60s"] = {
        "trades": len(trades), "base_volume": sum(r["volume"] for r in trades),
        "notional_usd": round(sum(r["volume"] * r["price"] for r in trades), 2),
        "first_trade_at_s": round(off(trades[0]), 2) if trades else None,
        "capture_covers_full_minute": duration >= W_VOLUME}

    # --- Cadence / silences ------------------------------------------------------------------
    first10 = [r for r in rows if off(r) <= 10]
    gaps = [(b["ts_ms"] - a["ts_ms"]) / 1000 for a, b in zip(rows, rows[1:])]
    out["cadence"] = {"ticks_per_s_first_10s": round(len(first10) / min(10, max(duration, 1e-9)), 1),
                      "max_gap_s": round(max(gaps), 3) if gaps else 0.0}
    return out


def format_report(d: dict) -> str:
    L = [f"=== Physique du listing — {d['file']} ===",
         f"Capture : {d['ticks']} ticks sur {d['duration_s']}s {d['kinds']} | prix de départ {d['first_price']:.8g}"]
    s = d["spread_10s"]
    if s:
        L += ["", "SPREAD Bid/Ask — 10 premières secondes",
              f"  écart max      : {s['max_pct']:.4f} %  (abs {s['max_abs']:.8g}, à t+{s['max_at_s']}s)",
              f"  médiane        : {s['median_pct']:.4f} %  sur {s['book_ticks']} mises à jour du carnet",
              f"  1er carnet     : {s['first_book_pct']:.4f} %  (à t+{s['first_book_at_s']}s)"]
    else:
        L += ["", "SPREAD Bid/Ask — n/a (aucun tick avec bid ET ask dans les 10 premières secondes)"]
    f = d["first_second"]
    L += ["", "HAUSSE — première seconde (réf. = 1er tick)",
          f"  pic            : {f['peak_rise_pct']:+.3f} %  (à t+{f['peak_at_s']}s)",
          f"  à t+1s         : {f['rise_at_1s_pct']:+.3f} %   | pire repli : {f['max_drawdown_pct']:+.3f} %   | {f['ticks']} ticks"]
    for k, v in (d.get("price_path") or {}).items():
        L.append(f"  à {k:<4}        : {v:+.3f} %")
    v = d["volume_60s"]
    L += ["", "VOLUME — première minute"]
    if v["trades"]:
        L += [f"  {v['trades']} trades | volume {v['base_volume']:,.4f} | notionnel ≈ {v['notional_usd']:,.2f} $ "
              f"| 1er trade à t+{v['first_trade_at_s']}s"]
    else:
        L += ["  n/a (aucun tick 'trade' avec volume dans l'enregistrement)"]
    if not v["capture_covers_full_minute"]:
        L += [f"  ⚠️ capture de {d['duration_s']}s < 60s : volume PARTIEL"]
    c = d["cadence"]
    L += ["", f"CADENCE : {c['ticks_per_s_first_10s']} ticks/s (10 premières s) | plus grand silence {c['max_gap_s']}s"]
    if "spread_overall" in d:
        L += [f"Spread sur toute la capture : médiane {d['spread_overall']['median_pct']:.4f} % / max {d['spread_overall']['max_pct']:.4f} %"]
    return "\n".join(L)


def run(path: str, as_json: bool = False) -> None:
    d = analyze(path)
    print(json.dumps(d, indent=2, ensure_ascii=False) if as_json else format_report(d))
