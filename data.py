import requests
import pandas as pd
import time

def get_klines(symbol: str, interval: str, limit: int = 100):
    """
    Récupère les chandelles depuis Binance avec retry
    """
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    
    for attempt in range(3):
        try:
            response = requests.get(url, params=params, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
                df = pd.DataFrame(data, columns=[
                    'open_time', 'open', 'high', 'low', 'close', 'volume',
                    'close_time', 'quote_asset_volume', 'number_of_trades',
                    'taker_buy_base_asset_volume', 
                    'taker_buy_quote_asset_volume', 'ignore'
                ])
                
                df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                
                print(f"✅ Données Binance récupérées ({len(df)} chandelles)")
                return df
                
            elif response.status_code == 429:
                print("⚠️ Rate limit Binance, attente 5s...")
                time.sleep(5)
            else:
                print(f"⚠️ Erreur Binance {response.status_code}")
                
        except Exception as e:
            print(f"❌ Tentative {attempt+1} échouée: {e}")
            time.sleep(3)
    
    print("❌ Impossible de récupérer les données Binance après 3 tentatives")
    return None