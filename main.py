import os
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from data import get_klines, LAST_SOURCE
from strategy import analyze_btc_signal

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

CHECK_INTERVAL_MINUTES = 8
COOLDOWN_SECONDS = 1800
MAX_SIGNALS_PER_DAY = 5

state = {
    "capital": float(os.getenv("CAPITAL", 5.0)),
    "last_signal_time": None,
    "signals_sent_today": 0,
    "current_day": datetime.now().date(),
}


def format_signal_message(signal: dict, capital: float) -> str:
    emoji = "🟢" if signal["direction"] == "LONG" else "🔴"
    return f"""{emoji} <b>SIGNAL BTCUSDT - {signal['direction']}</b>

<b>Heure :</b> {datetime.now().strftime('%H:%M')} WAT
<b>Prix d'entrée approx :</b> {signal['entry']} USDT
<b>SL :</b> {signal['sl']} USDT
<b>TP1 :</b> {signal['tp1']} USDT ({signal['rr1']}R)
<b>TP2 :</b> {signal['tp2']} USDT ({signal['rr2']}R)

<b>Risque max :</b> 1%
Avec ton capital ({capital}$) → <b>Lot suggéré :</b> {signal['suggested_lot']}
<b>Risque en USDT :</b> {signal['risk_usdt']}$

<b>Durée estimée :</b> 8 - 30 min
<b>Raison :</b> {signal['reason']}
"""


# ---------- Commandes Telegram ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot BTC Scalp prêt !\n"
        "Signaux automatiques activés + commandes :\n"
        "/signal - forcer une analyse maintenant\n"
        "/capital <montant> - mettre à jour ton capital\n"
        "/status - voir l'état du bot\n"
        "/debug - voir la source de données utilisée"
    )


async def set_capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        state["capital"] = float(context.args[0])
        await update.message.reply_text(f"✅ Capital mis à jour : {state['capital']}$")
    except (IndexError, ValueError):
        await update.message.reply_text("Utilise : /capital 10")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"💰 Capital : {state['capital']}$\n"
        f"📊 Signaux envoyés aujourd'hui : {state['signals_sent_today']}/{MAX_SIGNALS_PER_DAY}\n"
        f"⏱️ Vérification toutes les {CHECK_INTERVAL_MINUTES} min"
    )


async def force_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df_15m = get_klines("BTCUSDT", "15m", limit=100)
    df_1h = get_klines("BTCUSDT", "1h", limit=60)

    if df_15m is None or df_1h is None:
        await update.message.reply_text("⚠️ Impossible de récupérer les données pour le moment (Binance, Bybit et OKX indisponibles).")
        return

    signal = analyze_btc_signal(df_15m, df_1h, capital=state["capital"])

    if signal:
        await update.message.reply_text(format_signal_message(signal, state["capital"]), parse_mode="HTML")
    else:
        await update.message.reply_text("Aucun signal valide pour le moment.")


async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df_15m = get_klines("BTCUSDT", "15m", limit=5)

    if df_15m is None or df_15m.empty:
        await update.message.reply_text("❌ Aucune source (Binance/Bybit/OKX) n'a répondu à l'instant.")
        return

    last = df_15m.iloc[-1]
    source = LAST_SOURCE.get("name") or "inconnue"
    ts = LAST_SOURCE.get("time")
    ts_str = ts.strftime("%H:%M:%S UTC") if ts is not None else "N/A"

    msg = (
        f"🔍 <b>Debug données</b>\n"
        f"Source utilisée : <b>{source}</b>\n"
        f"Récupéré à : {ts_str}\n\n"
        f"Dernière bougie 15m :\n"
        f"🕒 {last['open_time']}\n"
        f"O: {last['open']} | H: {last['high']}\n"
        f"L: {last['low']} | C: {last['close']}\n"
        f"Volume: {last['volume']}"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ---------- Boucle automatique (remplace l'ancien while True) ----------

async def check_signal_job(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().date()
    if today != state["current_day"]:
        state["current_day"] = today
        state["signals_sent_today"] = 0

    if state["signals_sent_today"] >= MAX_SIGNALS_PER_DAY:
        return

    if not CHAT_ID:
        return

    try:
        df_15m = get_klines("BTCUSDT", "15m", limit=100)
        df_1h = get_klines("BTCUSDT", "1h", limit=60)

        if df_15m is None or df_1h is None:
            return

        signal = analyze_btc_signal(df_15m, df_1h, capital=state["capital"])
        if not signal:
            return

        current_time = datetime.now()
        last = state["last_signal_time"]
        if last is not None and (current_time - last).total_seconds() < COOLDOWN_SECONDS:
            return

        message = format_signal_message(signal, state["capital"])
        await context.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="HTML")

        state["last_signal_time"] = current_time
        state["signals_sent_today"] += 1
        print(f"[{current_time.strftime('%H:%M')}] Signal {signal['direction']} envoyé.")

    except Exception as e:
        print(f"Erreur dans check_signal_job: {e}")


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("⚠️ TELEGRAM_TOKEN manquant dans les variables d'environnement Railway")

    if not CHAT_ID:
        print("⚠️ CHAT_ID manquant : les commandes marcheront, mais pas les signaux automatiques.")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("capital", set_capital))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("signal", force_signal))
    app.add_handler(CommandHandler("debug", debug_cmd))

    app.job_queue.run_repeating(check_signal_job, interval=CHECK_INTERVAL_MINUTES * 60, first=15)

    print("🚀 BTC Scalp Bot démarré (commandes + signaux automatiques)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
