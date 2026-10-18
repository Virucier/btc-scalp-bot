"""Mise en forme (HTML) et envoi Telegram du résultat de crypto_analyzer.analyze()."""
import html
import os

import requests

TREND_EMOJI = {"HAUSSIÈRE": "🟢", "BAISSIÈRE": "🔴"}
REC_EMOJI = {"LONG": "🚀", "SHORT": "🔻"}


def _levels(values, n=2) -> str:
    return " | ".join(f"${v:,.1f}" for v in values[:n])


def format_telegram_message(data: dict) -> str:
    """Transforme le dict de `analyze()` en message HTML lisible sur Telegram mobile."""
    symbol = html.escape(data.get("symbol", "N/A"))
    exchange = html.escape(data.get("exchange", "N/A").upper())
    trend = data.get("global_trend", "NEUTRE").upper()
    rec = data.get("recommendation", {})
    position = rec.get("position", "NEUTRAL").upper()
    price = rec.get("entry", 0.0)

    lines = [
        f"<b>{REC_EMOJI.get(position, '⏳')} SIGNAL CRYPTO : {symbol} ({exchange})</b>",
        f"<i>Prix actuel : ${price:,.2f}</i>",
        "───────────────",
        f"<b>Tendance globale :</b> {TREND_EMOJI.get(trend, '⚪')} {trend}",
        f"<b>Action suggérée :</b> <u>{position}</u>",
        "",
    ]

    if position in ("LONG", "SHORT"):
        lines += [
            "🎯 <b>PLAN DE TRADE</b>",
            f"• <b>Entrée :</b> ${rec.get('entry', price):,.2f}",
            f"• <b>Stop-Loss :</b> ${rec['stop_loss']:,.2f} 🛑",
            f"• <b>Take-Profit :</b> ${rec['take_profit']:,.2f} 🎯",
            f"• <b>Ratio R:R :</b> 1:{rec.get('risk_reward', 2):.0f}",
            "",
        ]
    else:
        lines += ["🛡️ <b>Mise en garde :</b> Aucun signal clair détecté. Rester à l'écart du marché.", ""]

    lines.append("📊 <b>NIVEAUX CLÉS</b>")
    if data.get("resistances"):
        lines.append(f"• <b>Résistances :</b> {_levels(data['resistances'])}")
    if data.get("supports"):
        lines.append(f"• <b>Supports :</b> {_levels(data['supports'])}")

    lines += [
        "───────────────",
        "⚠️ <i>Gestion des risques : ne risquez jamais plus de 1-2% par trade.</i>",
    ]
    return "\n".join(lines)


def send_telegram_alert(text_message: str, bot_token: str | None = None, chat_id: str | None = None) -> dict:
    """Envoie le message via l'API Telegram (token/chat_id lus dans l'environnement par défaut)."""
    bot_token = bot_token or os.environ["TELEGRAM_TOKEN"]
    chat_id = chat_id or os.environ["TELEGRAM_CHAT_ID"]
    response = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text_message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    from crypto_analyzer import analyze

    msg = format_telegram_message(analyze())
    print(msg)
    if os.getenv("TELEGRAM_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        send_telegram_alert(msg)
