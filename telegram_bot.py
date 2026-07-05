import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv
from data import get_klines
from strategy import analyze_btc_signal, get_debug_info

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
user_capital = float(os.getenv("CAPITAL", 5.0))

def format_signal_message(signal: dict) -> str:
    emoji = "🟢" if signal["direction"] == "LONG" else "🔴"
    return f"""{emoji} <b>SIGNAL BTCUSDT - {signal['direction']}</b>

<b>Heure :</b> {__import__('datetime').datetime.now().strftime('%H:%M')} WAT
<b>Prix d'entrée approx :</b> {signal['entry']} USDT
<b>SL :</b> {signal['sl']} USDT
<b>TP1 :</b> {signal['tp1']} USDT ({signal['rr1']}R)
<b>TP2 :</b> {signal['tp2']} USDT ({signal['rr2']}R)
<b>Risque :</b> 1% → Lot suggéré : {signal['suggested_lot']} | Risque USDT : {signal['risk_usdt']}$
<b>Raison :</b> {signal['reason']}"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Bot BTC & ETH Scalp</b>\n\n"
        "Utilise /signal pour forcer un signal\n"
        "/debug pour voir l'état du marché"
    )

async def set_capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global user_capital
    try:
        user_capital = float(context.args[0])
        await update.message.reply_text(f"✅ Capital mis à jour : {user_capital}$")
    except:
        await update.message.reply_text("Utilise : /capital 10")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"💰 Capital : {user_capital}$")

async def force_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df_15m = get_klines("BTCUSDT", "15m", limit=100)
    df_1h = get_klines("BTCUSDT", "1h", limit=60)
    signal = analyze_btc_signal(df_15m, df_1h, capital=user_capital)
    if signal:
        await update.message.reply_text(format_signal_message(signal))
    else:
        await update.message.reply_text("Aucun signal valide pour le moment.")

async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df_15m = get_klines("BTCUSDT", "15m", limit=80)
    df_1h = get_klines("BTCUSDT", "1h", limit=50)

    if df_15m is None or df_1h is None:
        await update.message.reply_text("❌ Impossible de récupérer les données.")
        return

    debug_text = get_debug_info(df_15m, df_1h)
    await update.message.reply_text(debug_text, parse_mode="HTML")

app = Application.builder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("capital", set_capital))
app.add_handler(CommandHandler("status", status))
app.add_handler(CommandHandler("signal", force_signal))
app.add_handler(CommandHandler("debug", debug))

if __name__ == "__main__":
    app.run_polling()