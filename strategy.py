import pandas as pd
from bot.indicators import calculate_ema, detect_swing_highs_lows, check_liquidity_sweep

def analyze_btc_signal(df_15m: pd.DataFrame, df_1h: pd.DataFrame, capital: float = 5.0) -> dict | None:
    """
    Main strategy logic for BTC scalping.
    Returns a signal dict or None if no good setup.
    """
    if len(df_15m) < 50:
        return None

    df_15m = df_15m.copy()
    df_15m['ema9'] = calculate_ema(df_15m, 9)
    df_15m['ema21'] = calculate_ema(df_15m, 21)
    df_15m = detect_swing_highs_lows(df_15m, window=5)

    current_price = df_15m['close'].iloc[-1]
    ema9 = df_15m['ema9'].iloc[-1]
    ema21 = df_15m['ema21'].iloc[-1]

    if len(df_1h) < 30:
        return None

    df_1h = df_1h.copy()
    df_1h['ema50'] = calculate_ema(df_1h, 50)
    bias = "long" if current_price > df_1h['ema50'].iloc[-1] else "short"

    sweep = check_liquidity_sweep(df_15m, current_price, lookback=25)

    near_ema9 = abs(current_price - ema9) / current_price < 0.0035
    near_ema21 = abs(current_price - ema21) / current_price < 0.005

    avg_volume = df_15m['volume'].tail(20).mean()
    current_volume = df_15m['volume'].iloc[-1]
    volume_ok = current_volume > (avg_volume * 1.1)

    signal = None

    # LONG SETUP
    if (bias == "long" and 
        sweep == "long_sweep" and 
        (near_ema9 or near_ema21) and 
        volume_ok):

        sl_price = min(df_15m['low'].iloc[-3], ema21) * 0.998
        tp1 = current_price + (current_price - sl_price) * 1.6
        tp2 = current_price + (current_price - sl_price) * 2.8

        risk_percent = 0.01
        risk_amount = capital * risk_percent
        stop_distance = current_price - sl_price
        if stop_distance <= 0:
            return None

        suggested_lot = round(risk_amount / stop_distance, 4)

        signal = {
            "direction": "LONG",
            "entry": round(current_price, 1),
            "sl": round(sl_price, 1),
            "tp1": round(tp1, 1),
            "tp2": round(tp2, 1),
            "rr1": 1.6,
            "rr2": 2.8,
            "suggested_lot": max(suggested_lot, 0.001),
            "risk_usdt": round(risk_amount, 2),
            "reason": "Liquidity sweep bas + Retest EMA + Volume + Bias haussier H1"
        }

    # SHORT SETUP
    elif (bias == "short" and 
          sweep == "short_sweep" and 
          (near_ema9 or near_ema21) and 
          volume_ok):

        sl_price = max(df_15m['high'].iloc[-3], ema21) * 1.002
        tp1 = current_price - (sl_price - current_price) * 1.6
        tp2 = current_price - (sl_price - current_price) * 2.8

        risk_percent = 0.01
        risk_amount = capital * risk_percent
        stop_distance = sl_price - current_price
        if stop_distance <= 0:
            return None

        suggested_lot = round(risk_amount / stop_distance, 4)

        signal = {
            "direction": "SHORT",
            "entry": round(current_price, 1),
            "sl": round(sl_price, 1),
            "tp1": round(tp1, 1),
            "tp2": round(tp2, 1),
            "rr1": 1.6,
            "rr2": 2.8,
            "suggested_lot": max(suggested_lot, 0.001),
            "risk_usdt": round(risk_amount, 2),
            "reason": "Liquidity sweep haut + Retest EMA + Volume + Bias baissier H1"
        }

    return signal