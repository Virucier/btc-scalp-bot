"""Alertes Telegram pour les listings détectés par listing_sniper (HTML, dédoublonnées)."""
import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from telegram_format import send_telegram_alert

STATE_FILE = Path(os.getenv("LISTING_STATE_FILE", ".listing_seen.json"))


def _key(l) -> str:
    return f"{l.exchange}|{l.market}|{l.symbol}|{l.start:%Y-%m-%dT%H:%M}"


def countdown(start: datetime, time_known: bool = True, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if not time_known:
        return "heure exacte non communiquée"
    secs = int((start - now).total_seconds())
    if secs <= 0:
        return "déjà lancé" if secs > -60 else f"lancé il y a {_fmt(-secs)}"
    return f"dans {_fmt(secs)}"


def _fmt(secs: int) -> str:
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}j {h}h{m:02d}"
    return f"{h}h{m:02d}" if h else f"{m}m{s:02d}s"


def format_listing_alert(l, now: datetime | None = None) -> str:
    when = f"{l.start:%Y-%m-%d %H:%M} UTC" if l.time_known else f"{l.start:%Y-%m-%d} (heure ?)"
    lines = [
        f"<b>🆕 NOUVEAU LISTING : {html.escape(l.symbol)}</b>",
        f"<b>Exchange :</b> {html.escape(l.exchange)} ({html.escape(l.market)})",
        f"<b>Lancement :</b> {when}",
        f"<b>⏳ Compte à rebours :</b> {countdown(l.start, l.time_known, now)}",
    ]
    if l.title:
        lines.append(f"<i>{html.escape(l.title[:120])}</i>")
    if l.url:
        lines.append(f'<a href="{html.escape(l.url, quote=True)}">Annonce</a>')
    lines.append("⚠️ <i>Listing = volatilité extrême. Audit du contrat avant tout achat.</i>")
    return "\n".join(lines)


def _load() -> set:
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except (OSError, ValueError):
        return set()


def notify_new_listings(listings, dry_run: bool = False, state_file: Path | None = None) -> list:
    """Envoie une alerte pour chaque listing jamais vu ; retourne ceux qui ont été notifiés.
    Un listing n'est marqué « vu » qu'après un envoi réussi (sinon il est retenté au prochain scan)."""
    global STATE_FILE
    if state_file:
        STATE_FILE = state_file
    seen, sent = _load(), []
    for l in listings:
        k = _key(l)
        if k in seen:
            continue
        msg = format_listing_alert(l)
        if dry_run:
            print("--- [dry-run] message Telegram ---\n" + msg + "\n")
        else:
            try:
                send_telegram_alert(msg)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] envoi Telegram échoué pour {l.symbol}: {type(e).__name__}: {e}")
                continue
            seen.add(k)
        sent.append(l)
    if not dry_run:
        STATE_FILE.write_text(json.dumps(sorted(seen)))
    return sent


def notify_text(text: str, dry_run: bool = False) -> bool:
    """Message libre (événements de simulation). Ne lève pas d'exception."""
    if dry_run:
        print("--- [dry-run] message Telegram ---\n" + text + "\n")
        return True
    try:
        send_telegram_alert(text)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[warn] envoi Telegram échoué: {type(e).__name__}: {e}")
        return False


def telegram_configured() -> bool:
    return bool(os.getenv("TELEGRAM_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
