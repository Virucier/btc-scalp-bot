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
MAX_TRADE_DURATION_MINUTES = 45  # au-delà, on considère le trade "expiré" (ni TP ni SL touché)

SYMBOLS = ["BTCUSDT", "ETHUSDT"]

state = {
    "capital": float(os.getenv("CAPITAL", 5.0)),
    "last_signal_time": {symbol: None for symbol in SYMBOLS},
    "signals_sent_today": 0,
    "current_day": datetime.now().date(),
    "open_trades": [],  # chaque item: symbol, direction, entry, sl, tp1, tp2, opened_at
}


def format_signal_message(symbol: str, signal: dict, capital: float) -> str:
    emoji = "🟢" if signal["direction"] == "LONG" else "🔴"
    return f"""{emoji} <b>SIGNAL {symbol} - {signal['direction']}</b>

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
    pairs = ", ".join(SYMBOLS)
    await update.message.reply_text(
        "🤖 Bot Scalp prêt !\n"
        f"Paires suivies : {pairs}\n"
        "Signaux automatiques activés + commandes :\n"
        "/signal - forcer une analyse maintenant (toutes les paires)\n"
        "/capital <montant> - mettre à jour ton capital\n"
        "/status - voir l'état du bot\n"
        "/debug - voir la source de données utilisée\n"
        "/trades - voir les trades en cours de suivi"
    )


async def set_capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        state["capital"] = float(context.args[0])
        await update.message.reply_text(f"✅ Capital mis à jour : {state['capital']}$")
    except (IndexError, ValueError):
        await update.message.reply_text("Utilise : /capital 10")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs = ", ".join(SYMBOLS)
    await update.message.reply_text(
        f"💰 Capital : {state['capital']}$\n"
        f"📈 Paires : {pairs}\n"
        f"📊 Signaux envoyés aujourd'hui : {state['signals_sent_today']}/{MAX_SIGNALS_PER_DAY}\n"
        f"⏱️ Vérification toutes les {CHECK_INTERVAL_MINUTES} min"
    )


async def force_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for symbol in SYMBOLS:
        df_15m = get_klines(symbol, "15m", limit=100)
        df_1h = get_klines(symbol, "1h", limit=60)

        if df_15m is None or df_1h is None:
            await update.message.reply_text(f"⚠️ {symbol} : impossible de récupérer les données (Binance, Bybit et OKX indisponibles).")
            continue

        signal = analyze_btc_signal(df_15m, df_1h, capital=state["capital"])

        if signal:
            await update.message.reply_text(format_signal_message(symbol, signal, state["capital"]), parse_mode="HTML")
        else:
            await update.message.reply_text(f"{symbol} : aucun signal valide pour le moment.")


async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.args[0].upper() if context.args else "BTCUSDT"
    df_15m = get_klines(symbol, "15m", limit=5)

    if df_15m is None or df_15m.empty:
        await update.message.reply_text(f"❌ Aucune source (Binance/Bybit/OKX) n'a répondu pour {symbol}.")
        return

    last = df_15m.iloc[-1]
    source = LAST_SOURCE.get("name") or "inconnue"
    ts = LAST_SOURCE.get("time")
    ts_str = ts.strftime("%H:%M:%S UTC") if ts is not None else "N/A"

    msg = (
        f"🔍 <b>Debug données - {symbol}</b>\n"
        f"Source utilisée : <b>{source}</b>\n"
        f"Récupéré à : {ts_str}\n\n"
        f"Dernière bougie 15m :\n"
        f"🕒 {last['open_time']}\n"
        f"O: {last['open']} | H: {last['high']}\n"
        f"L: {last['low']} | C: {last['close']}\n"
        f"Volume: {last['volume']}"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def open_trades_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not state["open_trades"]:
        await update.message.reply_text("Aucun trade en cours de suivi actuellement.")
        return

    lines = ["📋 <b>Trades en cours de suivi</b>\n"]
    for t in state["open_trades"]:
        elapsed = int((datetime.now() - t["opened_at"]).total_seconds() / 60)
        lines.append(
            f"• {t['symbol']} {t['direction']} — entrée {t['entry']} "
            f"(SL {t['sl']} / TP1 {t['tp1']}) — depuis {elapsed} min"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------- Boucle automatique (remplace l'ancien while True) ----------

async def check_open_trades(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_ID or not state["open_trades"]:
        return

    still_open = []

    for trade in state["open_trades"]:
        symbol = trade["symbol"]
        df_15m = get_klines(symbol, "15m", limit=10)

        if df_15m is None or df_15m.empty:
            still_open.append(trade)  # on réessaiera au prochain cycle
            continue

        candles_after = df_15m[df_15m["open_time"] > trade["opened_at"]]

        outcome = None
        for _, candle in candles_after.iterrows():
            if trade["direction"] == "LONG":
                if candle["low"] <= trade["sl"]:
                    outcome = "loss"
                    break
                if candle["high"] >= trade["tp1"]:
                    outcome = "win"
                    break
            else:  # SHORT
                if candle["high"] >= trade["sl"]:
                    outcome = "loss"
                    break
                if candle["low"] <= trade["tp1"]:
                    outcome = "win"
                    break

        elapsed_minutes = (datetime.now() - trade["opened_at"]).total_seconds() / 60

        if outcome == "win":
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"✅ <b>Trade validé</b> — {symbol} {trade['direction']}\nTP1 atteint ({trade['tp1']} USDT)",
                parse_mode="HTML",
            )
        elif outcome == "loss":
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"❌ <b>Trade perdu</b> — {symbol} {trade['direction']}\nSL touché ({trade['sl']} USDT)",
                parse_mode="HTML",
            )
        elif elapsed_minutes >= MAX_TRADE_DURATION_MINUTES:
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"⏳ <b>Trade expiré</b> — {symbol} {trade['direction']}\nNi TP1 ni SL touché après {MAX_TRADE_DURATION_MINUTES} min.",
                parse_mode="HTML",
            )
        else:
            still_open.append(trade)  # toujours en cours, on garde

    state["open_trades"] = still_open

async def check_signal_job(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().date()
    if today != state["current_day"]:
        state["current_day"] = today
        state["signals_sent_today"] = 0

    if not CHAT_ID:
        return

    # 1) D'abord, on vérifie le résultat des trades déjà envoyés
    await check_open_trades(context)

    # 2) Ensuite, on cherche de nouveaux signaux
    for symbol in SYMBOLS:
        if state["signals_sent_today"] >= MAX_SIGNALS_PER_DAY:
            break

        try:
            df_15m = get_klines(symbol, "15m", limit=100)
            df_1h = get_klines(symbol, "1h", limit=60)

            if df_15m is None or df_1h is None:
                continue

            signal = analyze_btc_signal(df_15m, df_1h, capital=state["capital"])
            if not signal:
                continue

            current_time = datetime.now()
            last = state["last_signal_time"].get(symbol)
            if last is not None and (current_time - last).total_seconds() < COOLDOWN_SECONDS:
                continue

            message = format_signal_message(symbol, signal, state["capital"])
            await context.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="HTML")

            state["last_signal_time"][symbol] = current_time
            state["signals_sent_today"] += 1
            state["open_trades"].append({
                "symbol": symbol,
                "direction": signal["direction"],
                "entry": signal["entry"],
                "sl": signal["sl"],
                "tp1": signal["tp1"],
                "tp2": signal["tp2"],
                "opened_at": current_time,
            })
            print(f"[{current_time.strftime('%H:%M')}] Signal {signal['direction']} envoyé pour {symbol}.")

        except Exception as e:
            print(f"Erreur dans check_signal_job pour {symbol}: {e}")


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
    app.add_handler(CommandHandler("trades", open_trades_cmd))

    app.job_queue.run_repeating(check_signal_job, interval=CHECK_INTERVAL_MINUTES * 60, first=15)

    print("🚀 BTC Scalp Bot démarré (commandes + signaux automatiques)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
