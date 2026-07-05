import os
import time
import requests
from datetime import datetime
from dotenv import load_dotenv
from data import get_klines
from strategy import analyze_btc_signal

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
CAPITAL = float(os.getenv("CAPITAL", 5.0))
CHECK_INTERVAL_MINUTES = 8

last_signal_time = None


def send_telegram_message(message: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠️ TELEGRAM_TOKEN ou CHAT_ID manquant")
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Erreur Telegram: {e}")


def format_signal_message(signal: dict) -> str:
    emoji = "🟢" if signal["direction"] == "LONG" else "🔴"
    msg = f"""{emoji} <b>SIGNAL BTCUSDT - {signal['direction']}</b>

<b>Heure :</b> {datetime.now().strftime('%H:%M')} WAT
<b>Prix d'entrée approx :</b> {signal['entry']} USDT

<b>SL :</b> {signal['sl']} USDT
<b>TP1 :</b> {signal['tp1']} USDT   ({signal['rr1']}R)
<b>TP2 :</b> {signal['tp2']} USDT   ({signal['rr2']}R)

<b>Risque max :</b> 1%
Avec ton capital ({CAPITAL}$) → <b>Lot suggéré :</b> {signal['suggested_lot']}
<b>Risque en USDT :</b> {signal['risk_usdt']}$

<b>Durée estimée :</b> 8 - 30 min

<b>Raison :</b> {signal['reason']}
"""
    return msg


def run_bot():
    global last_signal_time, CAPITAL
    print("🚀 BTC Scalp Bot started...")

    while True:
        try:
            df_15m = get_klines("BTCUSDT", "15m", limit=100)
            df_1h = get_klines("BTCUSDT", "1h", limit=60)

            if df_15m is None or df_1h is None:
                time.sleep(120)
                continue

            signal = analyze_btc_signal(df_15m, df_1h, capital=CAPITAL)

            if signal:
                current_time = datetime.now()
                if last_signal_time is None or (current_time - last_signal_time).total_seconds() > 1800:
                    message = format_signal_message(signal)
                    print("\n" + "="*60)
                    print(message)
                    print("="*60)
                    send_telegram_message(message)
                    last_signal_time = current_time

            time.sleep(CHECK_INTERVAL_MINUTES * 60)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Erreur: {e}")
            time.sleep(120)


if __name__ == "__main__":
    run_bot()