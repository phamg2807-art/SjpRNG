import os
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

DB_URL = os.getenv('DATABASE_URL') or exit("ERROR: DATABASE_URL missing!")
_pool = None

def get_pool():
    global _pool
    if _pool is None or _pool.closed:
        _pool = ThreadedConnectionPool(2, 10, dsn=DB_URL, cursor_factory=RealDictCursor, sslmode='require', connect_timeout=10)
    return _pool

def get_conn():
    return get_pool().getconn()

def put_conn(conn):
    get_pool().putconn(conn)

# ─── Init ─────────────────────────────────────────────────────────────────────
def init_db():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Users
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    credits INT DEFAULT 0,
                    cash FLOAT DEFAULT 0.0,
                    joined_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS credits INT DEFAULT 0;")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS cash FLOAT DEFAULT 0.0;")

            # Coins
            cur.execute("""
                CREATE TABLE IF NOT EXISTS coins (
                    coin_id SERIAL PRIMARY KEY,
                    owner_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    material TEXT,
                    variant TEXT,
                    status TEXT,
                    float TEXT,
                    serial INT,
                    raw_value FLOAT,
                    final_value FLOAT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    for_sale BOOLEAN DEFAULT FALSE,
                    trade_locked BOOLEAN DEFAULT FALSE
                );
            """)

            # Bank
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bank (
                    id INT PRIMARY KEY DEFAULT 1,
                    balance FLOAT DEFAULT 0.0,
                    last_distributed TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("INSERT INTO bank (id, balance) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;")

            # Trades
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id SERIAL PRIMARY KEY,
                    sender_id BIGINT,
                    receiver_id BIGINT,
                    sender_coin_id INT,
                    receiver_coin_id INT,
                    cash_offer FLOAT DEFAULT 0.0,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)

            # Marketplace / Auctions
            cur.execute("""
                CREATE TABLE IF NOT EXISTS marketplace (
                    listing_id SERIAL PRIMARY KEY,
                    seller_id BIGINT,
                    coin_id INT,
                    starting_price FLOAT,
                    current_bid FLOAT DEFAULT 0.0,
                    highest_bidder BIGINT DEFAULT NULL,
                    ends_at TIMESTAMPTZ,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)

            # Transaction log
            cur.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    tx_id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    type TEXT,
                    amount FLOAT,
                    description TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)

        conn.commit()
        print("✅ Database initialized.")
    finally:
        put_conn(conn)

# ─── Helpers ──────────────────────────────────────────────────────────────────
def ensure_user(user_id, username):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, username) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
            """, (user_id, username))
        conn.commit()
    finally:
        put_conn(conn)

def award_credit(user_id, username):
    ensure_user(user_id, username)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET credits = credits + 1 WHERE user_id = %s", (user_id,))
        conn.commit()
    finally:
        put_conn(conn)

def get_user(user_id):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            return cur.fetchone()
    finally:
        put_conn(conn)

def get_bank():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM bank WHERE id = 1")
            return cur.fetchone()
    finally:
        put_conn(conn)

def add_to_bank(amount):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE bank SET balance = balance + %s WHERE id = 1", (amount,))
        conn.commit()
    finally:
        put_conn(conn)

def log_transaction(user_id, tx_type, amount, description):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO transactions (user_id, type, amount, description)
                VALUES (%s, %s, %s, %s)
            """, (user_id, tx_type, amount, description))
        conn.commit()
    finally:
        put_conn(conn)
