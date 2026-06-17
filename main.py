import os
import time
from threading import Thread
from flask import Flask
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

# -------------------- Flask Web Server (for Render) --------------------
app = Flask('SjpFish')

@app.route('/')
def home():
    return "SjpFish is alive!"

# Run Flask in a background thread
Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# -------------------- Environment & Database --------------------
DB_URL = os.getenv('DATABASE_URL') or exit("ERROR: DATABASE_URL missing!")
TOKEN  = os.getenv('DISCORD_TOKEN')  # kept for reference (not used in this stripped version)

_pool = None

def get_pool():
    global _pool
    if _pool is None or _pool.closed:
        _pool = ThreadedConnectionPool(
            4, 20,
            dsn=DB_URL,
            cursor_factory=RealDictCursor,
            sslmode='require',
            connect_timeout=10
        )
    return _pool

def db():
    """Get a connection from the pool."""
    return get_pool().getconn()

def release(conn):
    """Return a connection to the pool."""
    try:
        get_pool().putconn(conn)
    except Exception:
        pass

# -------------------- Keep the process alive --------------------
if __name__ == "__main__":
    while True:
        time.sleep(3600)   # idle loop – Render expects a long‑running process
