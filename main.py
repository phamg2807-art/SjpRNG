import os
import redis
import json
from flask import Flask
from threading import Thread
import discord
from discord.ext import commands
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

# ─── Flask Keep-Alive ─────────────────────────────────────────────────────────
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# ─── DB Pool ──────────────────────────────────────────────────────────────────
DB_URL = os.getenv('DATABASE_URL') or exit("ERROR: DATABASE_URL missing!")
_pool = None

def get_pool():
    global _pool
    if _pool is None or _pool.closed:
        _pool = ThreadedConnectionPool(
            2, 10,
            dsn=DB_URL,
            cursor_factory=RealDictCursor,
            sslmode='require',
            connect_timeout=10
        )
    return _pool

class _Conn:
    def __enter__(self):
        self.conn = get_pool().getconn()
        return self.conn
    def __exit__(self, exc_type, *_):
        if exc_type:
            self.conn.rollback()
        get_pool().putconn(self.conn)

def get_conn():
    return _Conn()

# ─── Redis Setup ──────────────────────────────────────────────────────────────
REDIS_URL = os.getenv('REDIS_URL') or exit("ERROR: REDIS_URL missing!")
rd = redis.from_url(REDIS_URL, decode_responses=True)

# ─── DB Init ──────────────────────────────────────────────────────────────────
def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS example (
                    id SERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
        conn.commit()
    print("✅ Database tables ready")

# ─── Bot Setup ────────────────────────────────────────────────────────────────
TOKEN = os.getenv('DISCORD_TOKEN') or exit("ERROR: DISCORD_TOKEN missing!")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='-', intents=intents, help_command=None)

# ─── Events ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    init_db()
    rd.set("test", "hello")
    print("Redis test:", rd.get("test"))  # should print: Redis test: hello
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

# ─── Run ──────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
