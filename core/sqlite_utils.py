import os
import sqlite3


def ensure_db_dir(path: str) -> None:
    db_dir = os.path.dirname(path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def connect_sqlite(path: str) -> sqlite3.Connection:
    ensure_db_dir(path)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
