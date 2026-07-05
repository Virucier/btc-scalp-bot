import pandas as pd
from indicators import calculate_ema, detect_swing_highs_lows, check_liquidity_sweep

def analyze_btc_signal(df_15m: pd.DataFrame, df_1h: pd.DataFrame, capital: float = 5.0) -> dict | None:
    if len(df_15m) < 50:
        print("Pas assez de données 15m")
        return None

    df_15m = df_15m.copy()
    df_15m['ema9'] = calculate_ema(df_15m, 9)
    df_15m['ema21'] = calculate_ema(df_15m, 21)
    df_15m = detect_swing_highs_lows(df_15m, window=5)

    current_price = df_15m['close'].iloc[-1]
    ema9 = df_15m['ema9'].iloc[-1]
    ema21 = df_15m['ema21'].iloc[-1]

    if len(df_1h) < 30:
        print("Pas assez de données 1H")
        return None

    df_1h = df_1h.copy()
    df_1h['ema50'] = calculate_ema(df_1h, 50)
    bias = "long" if current_price > df_1h['ema50'].iloc[-1] else "short"
    print(f"Bias 1H : {bias}")

    sweep = check_liquidity_sweep(df_15m, current_price, lookback=25)
    print(f"Liquidity Sweep détecté : {sweep}")

    near_ema9 = abs(current_price - ema9) / current_price < 0.0055
    near_ema21 = abs(current_price - ema21) / current_price < 0.0065
    print(f"Près EMA9 : {near_ema9} | Près EMA21 : {near_ema21}")

    # LONG
    if bias == "long" and sweep == "long_sweep" and (near_ema9 or near_ema21):
        print(">>> Signal LONG détecté !")
        # ... (le reste du code LONG reste identique)

    # SHORT
    elif bias == "short" and sweep == "short_sweep" and (near_ema9 or near_ema21):
        print(">>> Signal SHORT détecté !")
        # ... (le reste du code SHORT reste identique)

    return None  # On retourne None pour l'instant pour voir les logs