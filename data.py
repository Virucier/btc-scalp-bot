import requests
import pandas as pd
import time

def get_klines(symbol: str, interval: str, limit: int = 100):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    }

    for attempt in range(4):  # 4 tentatives
        try:
            response = requests.get(url, params=params, headers=headers, timeout=20)
            
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
                
                print(f"✅ Données récupérées ({len(df)} chandelles)")
                return df

            else:
                print(f"⚠️ Erreur {response.status_code} - Tentative {attempt+1}")
                
        except Exception as e:
            print(f"❌ Erreur tentative {attempt+1}: {e}")
        
        time.sleep(4)  # attend 4 secondes entre chaque essai

    print("❌ Échec après 4 tentatives")
    return None