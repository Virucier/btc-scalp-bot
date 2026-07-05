import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv
from bot.data import get_klines
from bot.strategy import analyze_btc_signal

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
user_capital = float(os.getenv("CAPITAL", 5.0))

def format_signal_message(signal: dict) -> str:
    emoji = "🟢" if signal["direction"] == "LONG" else "🔴"
    msg = f"""{emoji} <b>SIGNAL BTCUSDT - {signal['direction']}</b>

<b>Heure :</b> {__import__('datetime').datetime.now().strftime('%H:%M')} WAT
<b>Prix d'entrée approx :</b> {signal['entry']} USDT

<b>SL :</b> {signal['sl']} USDT
<b>TP1 :</b> {signal['tp1']} USDT   ({signal['rr1']}R)
<b>TP2 :</b> {signal['tp2']} USDT   ({signal['rr2']}R)

<b>Risque max :</b> 1%
Avec ton capital ({user_capital}$) → <b>Lot suggéré :</b> {signal['suggested_lot']}
<b>Risque en USDT :</b> {signal['risk_usdt']}$

<b>Durée estimée :</b> 8 - 30 min

<b>Raison :</b> {signal['reason']}
"""
    return msg

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>BTC Scalp Bot</b>\n\n"
        "Bienvenue ! Je t'envoie des signaux scalping sur BTCUSDT.\n\n"
        "Commandes :\n"
        "/capital &lt;montant&gt; → Ex: /capital 10\n"
        "/signal → Forcer une analyse\n"
        "/status → Voir ton capital",
        parse_mode="HTML"
    )

async def set_capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global user_capital
    try:
        new_capital = float(context.args[0])
        if new_capital < 1:
            await update.message.reply_text("❌ Capital minimum : 1$")
            return
        user_capital = new_capital
        await update.message.reply_text(f"✅ Capital mis à jour : {user_capital}$")
    except:
        await update.message.reply_text("Utilise : /capital 10")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"💰 Ton capital actuel : {user_capital}$")

async def force_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Analyse en cours...")

    df_15m = get_klines("BTCUSDT", "15m", limit=100)
    df_1h = get_klines("BTCUSDT", "1h", limit=60)

    if df_15m is None or df_1h is None:
        await update.message.reply_text("❌ Erreur lors de la récupération des données.")
        return

    signal = analyze_btc_signal(df_15m, df_1h, capital=user_capital)

    if signal:
        message = format_signal_message(signal)
        await update.message.reply_text(message)
    else:
        await update.message.reply_text("Aucun setup valide détecté pour le moment.")

def run_telegram_bot():
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN manquant")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("capital", set_capital))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("signal", force_signal))

    print("✅ Telegram bot running...")
    app.run_polling()

if __name__ == "__main__":
    run_telegram_bot()