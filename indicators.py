import pandas as pd
import numpy as np

def calculate_ema(df: pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    """Calculate Exponential Moving Average"""
    return df[column].ewm(span=period, adjust=False).mean()


def detect_swing_highs_lows(df: pd.DataFrame, window: int = 5) -> pd.DataFrame:
    """
    Detect recent swing highs and lows.
    A swing high is a candle higher than 'window' candles before and after it.
    """
    df = df.copy()
    df['swing_high'] = (
        (df['high'] == df['high'].rolling(window=window*2+1, center=True).max()) &
        (df['high'] > df['high'].shift(window)) &
        (df['high'] > df['high'].shift(-window))
    )
    
    df['swing_low'] = (
        (df['low'] == df['low'].rolling(window=window*2+1, center=True).max()) &
        (df['low'] < df['low'].shift(window)) &
        (df['low'] < df['low'].shift(-window))
    )
    return df


def get_recent_liquidity_levels(df: pd.DataFrame, lookback: int = 30) -> dict:
    """
    Get the most recent swing high and swing low (potential liquidity levels)
    """
    recent = df.tail(lookback)
    swing_highs = recent[recent['swing_high']]['high']
    swing_lows = recent[recent['swing_low']]['low']
    
    latest_swing_high = swing_highs.iloc[-1] if not swing_highs.empty else None
    latest_swing_low = swing_lows.iloc[-1] if not swing_lows.empty else None
    
    return {
        "latest_swing_high": latest_swing_high,
        "latest_swing_low": latest_swing_low
    }


def check_liquidity_sweep(df: pd.DataFrame, current_price: float, lookback: int = 20) -> str:
    """
    Check if price recently swept liquidity (broke recent swing high or low)
    Returns: 'long_sweep', 'short_sweep', or 'none'
    """
    levels = get_recent_liquidity_levels(df, lookback)
    
    if levels["latest