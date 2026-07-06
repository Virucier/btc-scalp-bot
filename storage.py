import os
import sqlite3
from contextlib import closing
from datetime import datetime

# Si un volume Railway est monté sur /data, on l'utilise (persistant).
# Sinon on retombe sur un fichier local (non persistant mais le bot ne plante pas).
DB_PATH = os.getenv("DB_PATH", "/data/bot.db")


def _get_path():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.isdir(db_dir):
        return "bot.db"
    return DB_PATH


def init_db():
    path = _get_path()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS open_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry REAL NOT NULL,
                sl REAL NOT NULL,
                tp1 REAL NOT NULL,
                tp2 REAL NOT NULL,
                opened_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry REAL NOT NULL,
                sl REAL NOT NULL,
                tp1 REAL NOT NULL,
                outcome TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT NOT NULL
            )
        """)
        conn.commit()
    persistent = os.path.isdir(os.path.dirname(DB_PATH)) if os.path.dirname(DB_PATH) else False
    print(f"💾 Base de données initialisée : {path} (persistant : {persistent})")


def load_open_trades():
    path = _get_path()
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM open_trades").fetchall()
    trades = []
    for row in rows:
        trades.append({
            "db_id": row["id"],
            "symbol": row["symbol"],
            "direction": row["direction"],
            "entry": row["entry"],
            "sl": row["sl"],
            "tp1": row["tp1"],
            "tp2": row["tp2"],
            "opened_at": datetime.fromisoformat(row["opened_at"]),
        })
    return trades


def add_open_trade(trade: dict) -> int:
    path = _get_path()
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute(
            "INSERT INTO open_trades (symbol, direction, entry, sl, tp1, tp2, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                trade["symbol"], trade["direction"], trade["entry"],
                trade["sl"], trade["tp1"], trade["tp2"],
                trade["opened_at"].isoformat(),
            ),
        )
        conn.commit()
        return cur.lastrowid


def close_trade(trade: dict, outcome: str):
    path = _get_path()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO trade_history (symbol, direction, entry, sl, tp1, outcome, opened_at, closed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade["symbol"], trade["direction"], trade["entry"], trade["sl"], trade["tp1"],
                outcome, trade["opened_at"].isoformat(), datetime.now().isoformat(),
            ),
        )
        if trade.get("db_id") is not None:
            conn.execute("DELETE FROM open_trades WHERE id = ?", (trade["db_id"],))
        conn.commit()


def get_history(limit: int = 10):
    path = _get_path()
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trade_history ORDER BY closed_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_win_rate():
    path = _get_path()
    with closing(sqlite3.connect(path)) as conn:
        total = conn.execute("SELECT COUNT(*) FROM trade_history WHERE outcome IN ('win','loss')").fetchone()[0]
        wins = conn.execute("SELECT COUNT(*) FROM trade_history WHERE outcome = 'win'").fetchone()[0]
    if total == 0:
        return None
    return round((wins / total) * 100, 1), wins, total
