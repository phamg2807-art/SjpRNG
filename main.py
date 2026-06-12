import os
import sys
import json
import time
import math
import random
import asyncio
import psycopg2
import discord
from flask import Flask
from threading import Thread
from datetime import datetime, timedelta, timezone
from discord.ext import commands, tasks
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

sys.stdout.reconfigure(line_buffering=True)

# ─── Flask Keep-Alive ──────────────────────────────────────────────────────────
app = Flask('')
@app.route('/')
def home(): return "CoinVault Bot is alive!"
Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# ─── Database ─────────────────────────────────────────────────────────────────
DB_URL = os.getenv('DATABASE_URL') or exit("ERROR: DATABASE_URL missing!")
TOKEN  = os.getenv('DISCORD_TOKEN')  or exit("ERROR: DISCORD_TOKEN missing!")

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

def db():
    return get_pool().getconn()

def release(conn):
    try:
        get_pool().putconn(conn)
    except Exception:
        pass

# ─── Game Constants ────────────────────────────────────────────────────────────
CRATE_COST         = 100
CRATE_FEE_PCT      = 0.05
TRADE_TAX_PCT      = 0.05
MARKET_FEE_PCT     = 0.08
CREDITS_PER_MSG    = 1
MSG_COOLDOWN_S     = 30
DAILY_CREDITS      = 50
DAILY_STREAK_BONUS = 5
WORK_COOLDOWN_H    = 4
WORK_MIN           = 10
WORK_MAX           = 40
ROB_COOLDOWN_H     = 6
ROB_SUCCESS_PCT    = 0.40
ROB_MAX_STEAL_PCT  = 0.20
ROB_FINE_PCT       = 0.15
GAMBLE_MIN         = 10
PRESTIGE_COST      = 5000

SHOP_ITEMS = {
    "rename":   {"cost": 200, "desc": "Rename one of your coins (cosmetic only)"},
    "polish":   {"cost": 150, "desc": "Upgrade a coin's Status by one tier"},
    "crate":    {"cost": 100, "desc": "Open a coin crate"},
    "crate_x3": {"cost": 270, "desc": "Open 3 crates at once (10% discount)"},
    "crate_x5": {"cost": 420, "desc": "Open 5 crates at once (16% discount)"},
}

# ─── Coin Attribute Tables ────────────────────────────────────────────────────
MATERIALS = [
    ("Plastic",   0.50,  10),
    ("Wood",      0.75,   8),
    ("Stone",     1.00,   6),
    ("Bronze",    1.20,   8),
    ("Copper",    1.25,   9),
    ("Iron",      1.50,  12),
    ("Steel",     1.60,  14),
    ("Gold",      3.00,  30),
    ("Aluminum",  1.75,  18),
    ("Carbon",    2.00,  20),
    ("Tungsten",  2.25,  25),
    ("Obsidian",  2.50,  30),
    ("Topaz",     2.30,  25),
    ("Diamond",   5.00, 100),
    ("Amethyst", 10.00, 250),
    ("Plasma",  100.00,2500),
]

VARIANTS = [
    ("Brown",      0.75,   4),
    ("Gray",       1.00,   2),
    ("Blue",       1.50,   6),
    ("Yellow",     1.75,  10),
    ("Black",      1.80,  12),
    ("White",      2.00,  15),
    ("Rainbow",    5.00,  50),
    ("Prismatic", 10.00, 200),
]

STATUSES = [
    ("Broken",    0.50,  12),
    ("Crushed",   0.60,  10),
    ("Oxidized",  0.75,   9),
    ("Scratched", 0.80,   8),
    ("Old",       0.90,   8),
    ("Like New",  0.95,   7),
    ("Normal",    1.00,   2),
    ("New",       1.25,   4),
    ("Sleek",     1.50,   8),
    ("Shiny",     1.75,  10),
    ("Modern",    2.00,  15),
    ("Elegant",   2.50,  20),
    ("Stunning",  2.75,  25),
]
STATUS_ORDER = ["Broken","Crushed","Oxidized","Scratched","Old","Like New","Normal","New","Sleek","Shiny","Modern","Elegant","Stunning"]

FLOATS = [
    ("Bad",       0.50,  15),
    ("Good",      1.00,   2),
    ("Great",     2.00,   4),
    ("Amazing",   3.00,   8),
    ("Heavenly", 15.00,  50),
    ("Godlike",  30.00, 100),
]

WORK_ACTIONS = [
    "polished some coins at the mint",
    "sorted crates at the warehouse",
    "delivered a rare coin shipment",
    "appraised coins for a collector",
    "ran the coin authentication desk",
    "helped catalog the vault archives",
    "guarded the treasury overnight",
    "tested the coin press machine",
    "cleaned the display cases",
    "audited the bank ledgers",
]

def weighted_choice(table):
    weights = [1.0 / d for _, _, d in table]
    total = sum(weights)
    r = random.uniform(0, total)
    cumulative = 0
    for (name, mult, _), w in zip(table, weights):
        cumulative += w
        if r <= cumulative:
            return name, mult
    return table[-1][0], table[-1][1]

def serial_multiplier(serial: int):
    s = serial
    if s > 0 and s % 1000 == 0: return s, 10.0
    if s == 9999:                return s, 10.0
    if "999" in str(s):          return s, 10.0
    if "99" in str(s):           return s, 10.0
    if s < 10:                   return s, 5.0
    if s < 100:                  return s, 3.0
    if s % 500 == 0 or s % 250 == 0 or s % 100 == 0: return s, 2.0
    return s, 1.0

def generate_coin():
    material,   mat_mult = weighted_choice(MATERIALS)
    variant,    var_mult = weighted_choice(VARIANTS)
    status,     sta_mult = weighted_choice(STATUSES)
    float_name, flt_mult = weighted_choice(FLOATS)
    serial_num = random.randint(0, 9999)
    _, ser_mult = serial_multiplier(serial_num)

    base_value  = round(random.uniform(1.0, 5.0), 2)
    total_mult  = mat_mult * var_mult * sta_mult * flt_mult * ser_mult
    final_value = round(base_value * total_mult, 4)

    return {
        "material": material, "variant": variant, "status": status,
        "float": float_name,  "serial": serial_num, "base_value": base_value,
        "mat_mult": mat_mult, "var_mult": var_mult, "sta_mult": sta_mult,
        "flt_mult": flt_mult, "ser_mult": ser_mult,
        "total_mult": round(total_mult, 4), "value": final_value,
    }

def coin_name(coin_row):
    return coin_row.get('custom_name') or f"{coin_row['variant']} {coin_row['material']} Coin"

def coin_display(coin_row, show_id=True):
    serial_str = str(coin_row['serial']).zfill(4)
    prefix = f"`#{coin_row['id']}` " if show_id and 'id' in coin_row else ""
    return (
        f"{prefix}**{coin_row['variant']} {coin_row['material']} Coin** #{serial_str}\n"
        f"  Status: **{coin_row['status']}** | Float: **{coin_row['float']}**\n"
        f"  Base: **${coin_row['base_value']:.2f}** × {coin_row['total_mult']:.4f} = **${coin_row['value']:.4f}**"
    )

def tier_emoji(value: float):
    if value >= 500: return "🌌"
    if value >= 100: return "👑"
    if value >= 50:  return "💎"
    if value >= 20:  return "🔥"
    if value >= 10:  return "⭐"
    if value >= 5:   return "🟡"
    if value >= 2:   return "🔵"
    if value >= 1:   return "⚪"
    return "🟤"

def coin_value_to_credits(value: float) -> int:
    return max(1, int(value * 100))

def prestige_multiplier(prestige: int) -> float:
    return 1.0 + (prestige * 0.1)

# ─── DB Init ──────────────────────────────────────────────────────────────────
def init_db():
    conn = db()
    cur = conn.cursor()
    
    # First, check if coins table exists and has id column
    cur.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables 
            WHERE table_name = 'coins'
        )
    """)
    coins_exists = cur.fetchone()['exists']
    
    if coins_exists:
        # Check if id column exists
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.columns 
                WHERE table_name = 'coins' AND column_name = 'id'
            )
        """)
        id_exists = cur.fetchone()['exists']
        
        if not id_exists:
            print("⚠️ coins table missing 'id' column! Adding it now...")
            # Drop and recreate coins table - this will preserve data by backing it up
            try:
                # Backup existing data
                cur.execute("CREATE TABLE IF NOT EXISTS coins_backup AS SELECT * FROM coins")
                # Drop old table
                cur.execute("DROP TABLE coins CASCADE")
                conn.commit()
                print("✅ Dropped old coins table, creating new one...")
            except Exception as e:
                print(f"Table drop error: {e}")
                conn.rollback()
    
    # Create tables with full schema
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id       BIGINT PRIMARY KEY,
            username      TEXT,
            credits       INT DEFAULT 0,
            last_msg_ts   BIGINT DEFAULT 0,
            last_daily    DATE,
            daily_streak  INT DEFAULT 0,
            last_work_ts  BIGINT DEFAULT 0,
            last_rob_ts   BIGINT DEFAULT 0,
            prestige      INT DEFAULT 0,
            total_coins   INT DEFAULT 0,
            joined_at     TIMESTAMP DEFAULT NOW()
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS coins (
            id          SERIAL PRIMARY KEY,
            owner_id    BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            material    TEXT NOT NULL,
            variant     TEXT NOT NULL,
            status      TEXT NOT NULL,
            float       TEXT NOT NULL,
            serial      INT NOT NULL,
            base_value  FLOAT DEFAULT 0,
            mat_mult    FLOAT DEFAULT 1,
            var_mult    FLOAT DEFAULT 1,
            sta_mult    FLOAT DEFAULT 1,
            flt_mult    FLOAT DEFAULT 1,
            ser_mult    FLOAT DEFAULT 1,
            total_mult  FLOAT DEFAULT 1,
            value       FLOAT DEFAULT 0,
            custom_name TEXT DEFAULT NULL,
            obtained_at TIMESTAMP DEFAULT NOW()
        )
    """)
    
    # Restore backup data if exists
    try:
        cur.execute("SELECT COUNT(*) as c FROM coins_backup")
        backup_count = cur.fetchone()['c']
        if backup_count > 0:
            print(f"🔄 Restoring {backup_count} coins from backup...")
            # Get column list from new table
            cur.execute("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'coins' ORDER BY ordinal_position
            """)
            new_cols = [row['column_name'] for row in cur.fetchall()]
            col_list = ', '.join(new_cols)
            
            cur.execute(f"""
                INSERT INTO coins ({col_list})
                SELECT {col_list} FROM coins_backup 
                WHERE owner_id IN (SELECT user_id FROM users)
            """)
            conn.commit()
            print("✅ Backup data restored!")
            
            # Drop backup table
            cur.execute("DROP TABLE IF EXISTS coins_backup")
            conn.commit()
    except Exception as e:
        print(f"Backup restore notice: {e}")
        conn.rollback()
    
    # Check and add missing columns to users table
    existing_user_cols = set()
    try:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='users'")
        existing_user_cols = {row['column_name'] for row in cur.fetchall()}
    except:
        pass
    
    user_columns = [
        ("last_msg_ts", "BIGINT DEFAULT 0"),
        ("last_work_ts", "BIGINT DEFAULT 0"),
        ("last_rob_ts", "BIGINT DEFAULT 0"),
        ("prestige", "INT DEFAULT 0"),
        ("total_coins", "INT DEFAULT 0"),
        ("daily_streak", "INT DEFAULT 0"),
        ("joined_at", "TIMESTAMP DEFAULT NOW()"),
        ("credits", "INT DEFAULT 0"),
    ]
    
    for col, col_def in user_columns:
        if col not in existing_user_cols:
            try:
                cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {col_def}")
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"User migration {col}: {e}")
    
    # Create other tables
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id            SERIAL PRIMARY KEY,
            initiator_id  BIGINT,
            receiver_id   BIGINT,
            coin_ids      TEXT,
            credits_offer INT DEFAULT 0,
            status        TEXT DEFAULT 'pending',
            created_at    TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS auctions (
            id           SERIAL PRIMARY KEY,
            seller_id    BIGINT,
            coin_id      INT,
            start_price  INT,
            current_bid  INT DEFAULT 0,
            bidder_id    BIGINT,
            ends_at      TIMESTAMP,
            status       TEXT DEFAULT 'active',
            created_at   TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bank (
            id    INT PRIMARY KEY DEFAULT 1,
            total INT DEFAULT 0
        )
    """)
    cur.execute("INSERT INTO bank(id,total) VALUES(1,0) ON CONFLICT (id) DO NOTHING")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bank_log (
            id        SERIAL PRIMARY KEY,
            source    TEXT,
            amount    INT,
            logged_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_log (
            paid_date DATE PRIMARY KEY,
            amount    INT,
            paid_at   TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS credit_log (
            id        SERIAL PRIMARY KEY,
            user_id   BIGINT,
            amount    INT,
            reason    TEXT,
            logged_at TIMESTAMP DEFAULT NOW()
        )
    """)
    
    conn.commit()
    
    # Verify the id column exists
    cur.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.columns 
            WHERE table_name = 'coins' AND column_name = 'id'
        )
    """)
    id_exists = cur.fetchone()['exists']
    if id_exists:
        print("✅ coins.id column verified!")
    else:
        print("❌ CRITICAL: coins.id column still missing!")
    
    release(conn)
    print("✅ Database initialized!")

# ─── Helpers ──────────────────────────────────────────────────────────────────
def ensure_user(user_id: int, username: str):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO users (user_id, username, credits, last_msg_ts, daily_streak,
                               last_work_ts, last_rob_ts, prestige, total_coins)
            VALUES (%s, %s, 0, 0, 0, 0, 0, 0, 0)
            ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
        """, (user_id, username))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"ensure_user error: {e}")
    finally:
        release(conn)

def get_user(user_id: int):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row:
            # Ensure all expected fields exist
            defaults = {
                'credits': 0, 'last_msg_ts': 0, 'last_work_ts': 0, 'last_rob_ts': 0,
                'prestige': 0, 'total_coins': 0, 'daily_streak': 0
            }
            for key, val in defaults.items():
                if key not in row or row[key] is None:
                    row[key] = val
        return row
    except Exception as e:
        print(f"get_user error: {e}")
        return None
    finally:
        release(conn)

def sync_coin_count(uid: int, cur):
    cur.execute(
        "UPDATE users SET total_coins = (SELECT COUNT(*) FROM coins WHERE owner_id = %s) WHERE user_id = %s",
        (uid, uid)
    )

def add_credits(user_id: int, amount: int, reason: str = ""):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET credits = credits + %s WHERE user_id = %s", (amount, user_id))
        if reason:
            cur.execute("INSERT INTO credit_log (user_id, amount, reason) VALUES (%s,%s,%s)", (user_id, amount, reason))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"add_credits error: {e}")
    finally:
        release(conn)

def get_bank():
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT total FROM bank WHERE id = 1")
        row = cur.fetchone()
        return row['total'] if row else 0
    finally:
        release(conn)

def count_users():
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) as c FROM users")
        row = cur.fetchone()
        return row['c'] if row else 1
    finally:
        release(conn)

def get_portfolio_value(uid: int) -> float:
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COALESCE(SUM(value), 0) as pv FROM coins WHERE owner_id = %s", (uid,))
        row = cur.fetchone()
        return float(row['pv']) if row else 0.0
    finally:
        release(conn)

# ─── Spam Prevention ──────────────────────────────────────────────────────────
MSG_COOLDOWNS = {}

# ─── Bot Setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='-', intents=intents, help_command=None)

# ─── Message Listener ─────────────────────────────────────────────────────────
@bot.event
async def on_message(message):
    if message.author.bot:
        return
    uid = message.author.id
    now = time.monotonic()
    ensure_user(uid, str(message.author))
    last = MSG_COOLDOWNS.get(uid, 0)
    if now - last >= MSG_COOLDOWN_S:
        MSG_COOLDOWNS[uid] = now
        u = get_user(uid)
        prestige_val = u['prestige'] if u and u.get('prestige') else 0
        bonus = int(CREDITS_PER_MSG * prestige_multiplier(prestige_val))
        add_credits(uid, bonus, "message")
    await bot.process_commands(message)

# ─── Background Tasks ─────────────────────────────────────────────────────────
@tasks.loop(minutes=5)
async def auction_checker():
    conn = db()
    cur = conn.cursor()
    try:
        now = datetime.now(timezone.utc)
        cur.execute("SELECT * FROM auctions WHERE status = 'active' AND ends_at <= %s", (now,))
        expired = cur.fetchall()

        for a in expired:
            coin_id   = a['coin_id']
            seller_id = a['seller_id']

            if a['bidder_id'] and a['current_bid']:
                winner_id  = a['bidder_id']
                sale_price = a['current_bid']
                fee        = int(round(sale_price * MARKET_FEE_PCT))
                seller_net = sale_price - fee

                cur.execute("UPDATE coins SET owner_id = %s WHERE id = %s", (winner_id, coin_id))
                cur.execute("UPDATE users SET credits = credits + %s WHERE user_id = %s", (seller_net, seller_id))
                cur.execute("UPDATE bank SET total = total + %s WHERE id = 1", (fee,))
                cur.execute("INSERT INTO bank_log (source, amount) VALUES (%s,%s)", (f"auction_fee:{a['id']}", fee))
                sync_coin_count(winner_id, cur)
                sync_coin_count(seller_id, cur)
                cur.execute("UPDATE auctions SET status = 'sold' WHERE id = %s", (a['id'],))
                conn.commit()

                try:
                    winner = await bot.fetch_user(winner_id)
                    conn2 = db()
                    cur2 = conn2.cursor()
                    cur2.execute("SELECT * FROM coins WHERE id = %s", (coin_id,))
                    c = cur2.fetchone()
                    release(conn2)
                    if winner and c:
                        await winner.send(f"🎉 You won auction **#{a['id']}**! **{coin_name(c)}** is now yours for **{sale_price:,} credits**.")
                except Exception:
                    pass
                try:
                    seller = await bot.fetch_user(seller_id)
                    if seller:
                        await seller.send(f"✅ Auction **#{a['id']}** sold for **{sale_price:,} credits**. You received **{seller_net:,}** after fees.")
                except Exception:
                    pass
            else:
                cur.execute("UPDATE coins SET owner_id = %s WHERE id = %s", (seller_id, coin_id))
                cur.execute("UPDATE auctions SET status = 'expired' WHERE id = %s", (a['id'],))
                conn.commit()
                try:
                    seller = await bot.fetch_user(seller_id)
                    if seller:
                        await seller.send(f"📦 Auction **#{a['id']}** expired with no bids. Coin returned.")
                except Exception:
                    pass
    except Exception as e:
        print(f"auction_checker error: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        release(conn)

@tasks.loop(hours=24)
async def daily_bank_distribution():
    bank_total = get_bank()
    if bank_total <= 0:
        return
    n = count_users()
    if n == 0:
        return
    share = bank_total // n
    if share <= 0:
        return

    conn = db()
    cur = conn.cursor()
    try:
        today = datetime.now(timezone.utc).date()
        cur.execute("SELECT paid_date FROM daily_log WHERE paid_date = %s", (today,))
        if cur.fetchone():
            return
        cur.execute("UPDATE users SET credits = credits + %s", (share,))
        cur.execute("UPDATE bank SET total = 0 WHERE id = 1")
        cur.execute("INSERT INTO daily_log (paid_date, amount) VALUES (%s, %s)", (today, share))
        conn.commit()
        print(f"✅ Daily bank payout: {share:,} credits to {n} users.")
    except Exception as e:
        conn.rollback()
        print(f"daily_bank_distribution error: {e}")
    finally:
        release(conn)

# ─── Trade View ───────────────────────────────────────────────────────────────
class TradeView(discord.ui.View):
    def __init__(self, trade_id: int, initiator_id: int, receiver_id: int):
        super().__init__(timeout=120)
        self.trade_id     = trade_id
        self.initiator_id = initiator_id
        self.receiver_id  = receiver_id

    @discord.ui.button(label="✅ Accept Trade", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.receiver_id:
            await interaction.response.send_message("❌ Only the trade recipient can accept.", ephemeral=True)
            return
        await self.resolve_trade(interaction, accepted=True)

    @discord.ui.button(label="❌ Decline Trade", style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.receiver_id, self.initiator_id):
            await interaction.response.send_message("❌ Not your trade.", ephemeral=True)
            return
        await self.resolve_trade(interaction, accepted=False)

    async def resolve_trade(self, interaction, accepted: bool):
        conn = db()
        cur = conn.cursor()
        try:
            cur.execute("SELECT * FROM trades WHERE id = %s AND status = 'pending'", (self.trade_id,))
            trade = cur.fetchone()

            if not trade:
                await interaction.response.send_message("⚠️ Trade no longer active.", ephemeral=True)
                return

            if not accepted:
                cur.execute("UPDATE trades SET status = 'declined' WHERE id = %s", (self.trade_id,))
                conn.commit()
                await interaction.response.edit_message(content="❌ Trade declined.", embed=None, view=None)
                self.stop()
                return

            coin_ids      = [int(x) for x in trade['coin_ids'].split(',') if x.strip()] if trade['coin_ids'] else []
            credits_offer = trade['credits_offer']

            if coin_ids:
                cur.execute("SELECT id, owner_id FROM coins WHERE id = ANY(%s)", (coin_ids,))
                rows = cur.fetchall()
                for r in rows:
                    if r['owner_id'] != trade['initiator_id']:
                        cur.execute("UPDATE trades SET status = 'invalid' WHERE id = %s", (self.trade_id,))
                        conn.commit()
                        await interaction.response.edit_message(content="❌ Coin ownership changed; trade cancelled.", embed=None, view=None)
                        self.stop()
                        return

            if credits_offer > 0:
                cur.execute("SELECT credits FROM users WHERE user_id = %s", (trade['initiator_id'],))
                init_user = cur.fetchone()
                if not init_user or init_user['credits'] < credits_offer:
                    cur.execute("UPDATE trades SET status = 'invalid' WHERE id = %s", (self.trade_id,))
                    conn.commit()
                    await interaction.response.edit_message(content="❌ Initiator has insufficient credits.", embed=None, view=None)
                    self.stop()
                    return

            if coin_ids:
                cur.execute("UPDATE coins SET owner_id = %s WHERE id = ANY(%s)", (trade['receiver_id'], coin_ids))

            if credits_offer > 0:
                tax = int(round(credits_offer * TRADE_TAX_PCT))
                net = credits_offer - tax
                cur.execute("UPDATE users SET credits = credits - %s WHERE user_id = %s", (credits_offer, trade['initiator_id']))
                cur.execute("UPDATE users SET credits = credits + %s WHERE user_id = %s", (net, trade['receiver_id']))
                cur.execute("UPDATE bank SET total = total + %s WHERE id = 1", (tax,))
                cur.execute("INSERT INTO bank_log (source, amount) VALUES (%s,%s)", (f"trade_tax:{self.trade_id}", tax))

            for uid in (trade['initiator_id'], trade['receiver_id']):
                sync_coin_count(uid, cur)

            cur.execute("UPDATE trades SET status = 'completed' WHERE id = %s", (self.trade_id,))
            conn.commit()

            embed = discord.Embed(title="✅ Trade Completed!", color=discord.Color.green())
            embed.description = f"Trade **#{self.trade_id}** accepted and processed."
            await interaction.response.edit_message(embed=embed, view=None)
            self.stop()
        except Exception as e:
            conn.rollback()
            print(f"resolve_trade error: {e}")
            try:
                await interaction.response.send_message("❌ An error occurred processing the trade.", ephemeral=True)
            except Exception:
                pass
        finally:
            release(conn)

# ─── Auction Bid Modal/View ───────────────────────────────────────────────────
class BidModal(discord.ui.Modal, title="Place a Bid"):
    amount = discord.ui.TextInput(label="Bid Amount (credits)", placeholder="e.g. 500")

    def __init__(self, auction_id: int):
        super().__init__()
        self.auction_id = auction_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            bid = int(self.amount.value)
        except ValueError:
            await interaction.response.send_message("❌ Enter a whole number of credits.", ephemeral=True)
            return

        uid = interaction.user.id
        ensure_user(uid, str(interaction.user))

        conn = db()
        cur = conn.cursor()
        try:
            cur.execute("SELECT * FROM auctions WHERE id = %s AND status = 'active'", (self.auction_id,))
            a = cur.fetchone()

            if not a:
                await interaction.response.send_message("❌ Auction not found or ended.", ephemeral=True)
                return
            if uid == a['seller_id']:
                await interaction.response.send_message("❌ You can't bid on your own auction.", ephemeral=True)
                return

            min_bid = max(a['start_price'], (a['current_bid'] or 0) + 1)
            if bid < min_bid:
                await interaction.response.send_message(f"❌ Minimum bid is **{min_bid:,} credits**.", ephemeral=True)
                return

            cur.execute("SELECT credits FROM users WHERE user_id = %s", (uid,))
            user = cur.fetchone()
            if not user or user['credits'] < bid:
                bal = user['credits'] if user else 0
                await interaction.response.send_message(f"❌ Insufficient credits. You have **{bal:,}**.", ephemeral=True)
                return

            if a['bidder_id'] and a['current_bid']:
                cur.execute("UPDATE users SET credits = credits + %s WHERE user_id = %s", (a['current_bid'], a['bidder_id']))

            cur.execute("UPDATE users SET credits = credits - %s WHERE user_id = %s", (bid, uid))
            cur.execute("UPDATE auctions SET current_bid = %s, bidder_id = %s WHERE id = %s",
                        (bid, uid, self.auction_id))
            conn.commit()

            await interaction.response.send_message(
                f"✅ Bid of **{bid:,} credits** placed on auction **#{self.auction_id}**!", ephemeral=True
            )
        except Exception as e:
            conn.rollback()
            print(f"BidModal error: {e}")
            try:
                await interaction.response.send_message("❌ An error occurred placing your bid.", ephemeral=True)
            except Exception:
                pass
        finally:
            release(conn)

class AuctionView(discord.ui.View):
    def __init__(self, auction_id: int):
        super().__init__(timeout=None)
        self.auction_id = auction_id

    @discord.ui.button(label="💰 Place Bid", style=discord.ButtonStyle.blurple)
    async def bid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BidModal(self.auction_id))

# ─── Gamble Views ─────────────────────────────────────────────────────────────
class CoinflipView(discord.ui.View):
    def __init__(self, uid: int, bet: int):
        super().__init__(timeout=30)
        self.uid = uid
        self.bet = bet

    @discord.ui.button(label="🪙 Heads", style=discord.ButtonStyle.blurple)
    async def heads(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("❌ Not your game.", ephemeral=True)
            return
        await self.resolve(interaction, "heads")

    @discord.ui.button(label="🪙 Tails", style=discord.ButtonStyle.grey)
    async def tails(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("❌ Not your game.", ephemeral=True)
            return
        await self.resolve(interaction, "tails")

    async def resolve(self, interaction: discord.Interaction, choice: str):
        result = random.choice(["heads", "tails"])
        won = (choice == result)

        conn = db()
        cur = conn.cursor()
        try:
            if won:
                cur.execute("UPDATE users SET credits = credits + %s WHERE user_id = %s", (self.bet, self.uid))
                bank_cut = max(1, int(self.bet * 0.02))
                cur.execute("UPDATE bank SET total = total + %s WHERE id = 1", (bank_cut,))
                cur.execute("INSERT INTO bank_log (source,amount) VALUES ('gamble_tax',%s)", (bank_cut,))
                color = discord.Color.green()
                title = "🎉 You Won!"
                desc  = f"The coin landed **{result}**! You win **{self.bet:,} credits**."
            else:
                cur.execute("UPDATE users SET credits = GREATEST(0, credits - %s) WHERE user_id = %s", (self.bet, self.uid))
                bank_cut = max(1, int(self.bet * 0.50))
                cur.execute("UPDATE bank SET total = total + %s WHERE id = 1", (bank_cut,))
                cur.execute("INSERT INTO bank_log (source,amount) VALUES ('gamble_house',%s)", (bank_cut,))
                color = discord.Color.red()
                title = "💸 You Lost!"
                desc  = f"The coin landed **{result}**. You lose **{self.bet:,} credits**."
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"CoinflipView error: {e}")
            color, title, desc = discord.Color.red(), "Error", "Something went wrong."
        finally:
            release(conn)

        embed = discord.Embed(title=title, description=desc, color=color)
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

# ─── EVENTS ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    init_db()
    if not auction_checker.is_running():
        auction_checker.start()
    if not daily_bank_distribution.is_running():
        daily_bank_distribution.start()
    print(f"✅ CoinVault logged in as {bot.user}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Missing argument. Use `-help` for usage.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Bad argument type. Use `-help` for usage.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    elif isinstance(error, commands.CommandInvokeError):
        print(f"CommandInvokeError in {ctx.command}: {error.original}")
        await ctx.send(f"❌ An error occurred running that command. Please try again.")
    else:
        print(f"Unhandled error: {error}")

# ─── COMMANDS ─────────────────────────────────────────────────────────────────

@bot.command()
async def help(ctx):
    e = discord.Embed(title="🪙 CoinVault — Command Reference", color=0x5865F2)
    e.add_field(name="💰 Economy (Credits)", value=(
        "`-balance` — Your credits & stats\n"
        "`-daily` — Claim daily credits (streak bonuses!)\n"
        "`-work` — Work for credits (4h cooldown)\n"
        "`-rob @user` — Attempt to rob someone (6h cooldown)\n"
        "`-gamble <amount>` — Coinflip bet\n"
        "`-slots <amount>` — Spin the slot machine\n"
        "`-prestige` — Spend 5,000 credits to prestige (+10% all earnings)\n"
    ), inline=False)
    e.add_field(name="🛒 Shop", value=(
        "`-shop` — Browse the credit shop\n"
        "`-buy crate` — 100 credits → open a crate\n"
        "`-buy crate_x3` — 270 credits → 3 crates\n"
        "`-buy crate_x5` — 420 credits → 5 crates\n"
        "`-buy polish <coin_id>` — 150 credits → upgrade coin status\n"
        "`-buy rename <coin_id> <name>` — 200 credits → rename coin\n"
    ), inline=False)
    e.add_field(name="🎒 Inventory", value=(
        "`-inventory [page]` / `-inv` — View your coins\n"
        "`-coin <id>` — Detailed coin view\n"
        "`-sell <coin_id>` — Sell coin for credits\n"
        "`-sellall` — Sell all coins (get credits)\n"
    ), inline=False)
    e.add_field(name="🤝 Trading", value=(
        "`-trade @user [coin_ids] [credits:<amount>]` — Offer trade\n"
        "  e.g. `-trade @Bob 12,15 credits:500`\n"
        "`-trades` — Your pending trades\n"
    ), inline=False)
    e.add_field(name="🏪 Marketplace", value=(
        "`-market [page]` — Browse auctions\n"
        "`-auction <coin_id> <start_price> [hours]` — List coin (8% fee)\n"
        "`-bid <auction_id>` — Bid on an auction\n"
        "`-myauctions` — Your active listings\n"
        "`-cancelauction <id>` — Cancel your auction\n"
    ), inline=False)
    e.add_field(name="📊 Stats & Social", value=(
        "`-profile [@user]` — View profile\n"
        "`-leaderboard` / `-lb` — Top by portfolio value\n"
        "`-richlist` — Top by credits held\n"
        "`-bank` — View bank treasury\n"
        "`-stats` — Your detailed stats\n"
    ), inline=False)
    e.set_footer(text="Credits are the ONLY currency • Earn from messages, daily, work, gamble, trading & selling coins")
    await ctx.send(embed=e)

@bot.command(aliases=['bal', 'wallet'])
async def balance(ctx):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    u = get_user(uid)
    if not u:
        await ctx.send("❌ Could not load your profile. Try again.")
        return

    portfolio = get_portfolio_value(uid)
    prestige_val = u.get('prestige') or 0
    pmult = prestige_multiplier(prestige_val)

    e = discord.Embed(title=f"💳 {ctx.author.display_name}'s Balance", color=0x57F287)
    e.add_field(name="🎟️ Credits",        value=f"**{u['credits']:,}**",                   inline=True)
    e.add_field(name="🪙 Coins Owned",     value=f"**{u['total_coins']}**",                  inline=True)
    e.add_field(name="📈 Portfolio Value", value=f"**${portfolio:.4f}**",                    inline=True)
    e.add_field(name="⭐ Prestige",        value=f"**{prestige_val}** (×{pmult:.1f} earnings)", inline=True)
    e.add_field(name="🔥 Daily Streak",    value=f"**{u.get('daily_streak', 0)}** days",     inline=True)
    await ctx.send(embed=e)

@bot.command()
async def daily(ctx):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    u = get_user(uid)
    if not u:
        await ctx.send("❌ Could not load your profile. Try again.")
        return

    today = datetime.now(timezone.utc).date()
    last_daily = u.get('last_daily')

    if last_daily and last_daily == today:
        tomorrow = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        diff = tomorrow - datetime.now(timezone.utc)
        h, rem = divmod(int(diff.total_seconds()), 3600)
        m = rem // 60
        await ctx.send(f"⏳ Already claimed today! Come back in **{h}h {m}m**.")
        return

    yesterday = today - timedelta(days=1)
    streak = (u.get('daily_streak') or 0) + 1 if last_daily == yesterday else 1
    streak_bonus = min(streak - 1, 7) * DAILY_STREAK_BONUS
    prestige_val = u.get('prestige') or 0
    prestige_mult = prestige_multiplier(prestige_val)
    total = int((DAILY_CREDITS + streak_bonus) * prestige_mult)

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE users SET credits = credits + %s, last_daily = %s, daily_streak = %s WHERE user_id = %s",
            (total, today, streak, uid)
        )
        cur.execute("INSERT INTO credit_log (user_id, amount, reason) VALUES (%s,%s,'daily')", (uid, total))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"daily error: {e}")
        await ctx.send("❌ An error occurred. Try again.")
        return
    finally:
        release(conn)

    e = discord.Embed(title="📅 Daily Claim!", color=0x57F287)
    e.add_field(name="💰 Credits Received", value=f"**{total:,}**",      inline=True)
    e.add_field(name="🔥 Streak",           value=f"**{streak}** day(s)", inline=True)
    if streak_bonus:
        e.add_field(name="🎁 Streak Bonus", value=f"+{streak_bonus} credits", inline=True)
    if prestige_mult > 1.0:
        e.add_field(name="⭐ Prestige Bonus", value=f"×{prestige_mult:.1f}", inline=True)
    e.set_footer(text="Come back tomorrow to keep your streak! Max streak bonus at day 8.")
    await ctx.send(embed=e)

@bot.command()
async def work(ctx):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    u = get_user(uid)
    if not u:
        await ctx.send("❌ Could not load your profile. Try again.")
        return

    now_ts = int(time.time())
    cooldown_s = WORK_COOLDOWN_H * 3600
    elapsed = now_ts - (u.get('last_work_ts') or 0)

    if elapsed < cooldown_s:
        remaining = cooldown_s - elapsed
        h, rem = divmod(remaining, 3600)
        m = rem // 60
        await ctx.send(f"⏳ You're tired! Work again in **{h}h {m}m**.")
        return

    earned = random.randint(WORK_MIN, WORK_MAX)
    prestige_val = u.get('prestige') or 0
    earned = int(earned * prestige_multiplier(prestige_val))
    action = random.choice(WORK_ACTIONS)

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE users SET credits = credits + %s, last_work_ts = %s WHERE user_id = %s",
            (earned, now_ts, uid)
        )
        cur.execute("INSERT INTO credit_log (user_id, amount, reason) VALUES (%s,%s,'work')", (uid, earned))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"work error: {e}")
        await ctx.send("❌ An error occurred. Try again.")
        return
    finally:
        release(conn)

    e = discord.Embed(title="💼 Work Complete!", color=0x57F287)
    e.description = f"{ctx.author.display_name} {action} and earned **{earned:,} credits**!"
    e.set_footer(text=f"Next work available in {WORK_COOLDOWN_H} hours")
    await ctx.send(embed=e)

@bot.command()
async def rob(ctx, target: discord.Member):
    if target.bot or target.id == ctx.author.id:
        await ctx.send("❌ Invalid target.")
        return

    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    ensure_user(target.id, str(target))

    u = get_user(uid)
    if not u:
        await ctx.send("❌ Could not load your profile. Try again.")
        return

    now_ts = int(time.time())
    cooldown_s = ROB_COOLDOWN_H * 3600
    elapsed = now_ts - (u.get('last_rob_ts') or 0)

    if elapsed < cooldown_s:
        remaining = cooldown_s - elapsed
        h, rem = divmod(remaining, 3600)
        m = rem // 60
        await ctx.send(f"⏳ Lay low! Rob again in **{h}h {m}m**.")
        return

    t = get_user(target.id)
    if not t or t['credits'] < 50:
        await ctx.send(f"❌ **{target.display_name}** doesn't have enough credits to rob (need at least 50).")
        return

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET last_rob_ts = %s WHERE user_id = %s", (now_ts, uid))

        if random.random() < ROB_SUCCESS_PCT:
            steal_amount = int(t['credits'] * random.uniform(0.05, ROB_MAX_STEAL_PCT))
            steal_amount = max(1, steal_amount)
            cur.execute("UPDATE users SET credits = GREATEST(0, credits - %s) WHERE user_id = %s", (steal_amount, target.id))
            cur.execute("UPDATE users SET credits = credits + %s WHERE user_id = %s", (steal_amount, uid))
            conn.commit()
            e = discord.Embed(title="🦹 Successful Robbery!", color=discord.Color.green())
            e.description = f"You slipped **{steal_amount:,} credits** from **{target.display_name}**!"
        else:
            fine = max(1, int((u.get('credits') or 0) * ROB_FINE_PCT))
            cur.execute("UPDATE users SET credits = GREATEST(0, credits - %s) WHERE user_id = %s", (fine, uid))
            cur.execute("UPDATE bank SET total = total + %s WHERE id = 1", (fine,))
            cur.execute("INSERT INTO bank_log (source,amount) VALUES ('rob_fine',%s)", (fine,))
            conn.commit()
            e = discord.Embed(title="🚔 Caught Red-Handed!", color=discord.Color.red())
            e.description = f"You got caught trying to rob **{target.display_name}** and paid a **{fine:,} credit** fine!"
    except Exception as ex:
        conn.rollback()
        print(f"rob error: {ex}")
        await ctx.send("❌ An error occurred. Try again.")
        return
    finally:
        release(conn)

    await ctx.send(embed=e)

@bot.command()
async def gamble(ctx, amount: int):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    u = get_user(uid)
    if not u:
        await ctx.send("❌ Could not load your profile. Try again.")
        return

    if amount < GAMBLE_MIN:
        await ctx.send(f"❌ Minimum bet is **{GAMBLE_MIN:,} credits**.")
        return
    if amount > u['credits']:
        await ctx.send(f"❌ Not enough credits. You have **{u['credits']:,}**.")
        return

    e = discord.Embed(
        title="🪙 Coinflip Gamble",
        description=f"Betting **{amount:,} credits** — pick a side!",
        color=0xFEE75C
    )
    view = CoinflipView(uid, amount)
    await ctx.send(embed=e, view=view)

SLOT_SYMBOLS = ["🍒","🍊","🍋","🍇","💎","🌟","🎰"]
SLOT_PAYOUTS = {
    ("💎","💎","💎"): 20,
    ("🌟","🌟","🌟"): 15,
    ("🎰","🎰","🎰"): 50,
    ("🍇","🍇","🍇"):  8,
    ("🍒","🍒","🍒"):  5,
    ("🍊","🍊","🍊"):  4,
    ("🍋","🍋","🍋"):  3,
}

@bot.command()
async def slots(ctx, amount: int):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    u = get_user(uid)
    if not u:
        await ctx.send("❌ Could not load your profile. Try again.")
        return

    if amount < GAMBLE_MIN:
        await ctx.send(f"❌ Minimum bet is **{GAMBLE_MIN:,} credits**.")
        return
    if amount > u['credits']:
        await ctx.send(f"❌ You have **{u['credits']:,} credits**.")
        return

    weights = [30, 25, 25, 15, 5, 3, 2]
    reel = random.choices(SLOT_SYMBOLS, weights=weights, k=3)
    result_key = tuple(reel)
    multiplier = SLOT_PAYOUTS.get(result_key, 0)

    conn = db()
    cur = conn.cursor()
    try:
        if multiplier > 0:
            winnings = amount * multiplier
            net = winnings - amount
            cur.execute("UPDATE users SET credits = credits + %s WHERE user_id = %s", (net, uid))
            bank_cut = max(1, int(amount * 0.02))
            cur.execute("UPDATE bank SET total = total + %s WHERE id = 1", (bank_cut,))
            cur.execute("INSERT INTO bank_log (source,amount) VALUES ('slots_tax',%s)", (bank_cut,))
            color = discord.Color.gold()
            result_line = f"🎉 **{' | '.join(reel)}** — **{multiplier}×** payout!\nWon **{winnings:,}** (net +**{net:,}**)"
        else:
            cur.execute("UPDATE users SET credits = GREATEST(0, credits - %s) WHERE user_id = %s", (amount, uid))
            bank_cut = max(1, int(amount * 0.50))
            cur.execute("UPDATE bank SET total = total + %s WHERE id = 1", (bank_cut,))
            cur.execute("INSERT INTO bank_log (source,amount) VALUES ('slots_house',%s)", (bank_cut,))
            color = discord.Color.red()
            result_line = f"💸 **{' | '.join(reel)}** — No match. Lost **{amount:,} credits**."
        conn.commit()
    except Exception as ex:
        conn.rollback()
        print(f"slots error: {ex}")
        await ctx.send("❌ An error occurred. Try again.")
        return
    finally:
        release(conn)

    e = discord.Embed(title="🎰 Slot Machine", description=result_line, color=color)
    e.set_footer(text="3× 🎰 = 50x | 3× 💎 = 20x | 3× 🌟 = 15x | 3× 🍇 = 8x | ...")
    await ctx.send(embed=e)

@bot.command()
async def prestige(ctx):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    u = get_user(uid)
    if not u:
        await ctx.send("❌ Could not load your profile. Try again.")
        return

    if u['credits'] < PRESTIGE_COST:
        await ctx.send(f"❌ Prestige costs **{PRESTIGE_COST:,} credits**. You have **{u['credits']:,}**.")
        return

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE users SET credits = credits - %s, prestige = prestige + 1 WHERE user_id = %s",
            (PRESTIGE_COST, uid)
        )
        cur.execute("UPDATE bank SET total = total + %s WHERE id=1", (PRESTIGE_COST // 2,))
        cur.execute("INSERT INTO bank_log (source,amount) VALUES ('prestige',%s)", (PRESTIGE_COST // 2,))
        conn.commit()
    except Exception as ex:
        conn.rollback()
        print(f"prestige error: {ex}")
        await ctx.send("❌ An error occurred. Try again.")
        return
    finally:
        release(conn)

    new_prestige = (u.get('prestige') or 0) + 1
    new_mult = prestige_multiplier(new_prestige)
    e = discord.Embed(title="⭐ PRESTIGE UNLOCKED!", color=0xFFD700)
    e.description = (
        f"**{ctx.author.display_name}** has reached **Prestige {new_prestige}**!\n"
        f"All credit earnings are now **×{new_mult:.1f}** permanently."
    )
    e.set_footer(text=f"Cost: {PRESTIGE_COST:,} credits • Half went to the bank treasury")
    await ctx.send(embed=e)

@bot.command()
async def shop(ctx):
    e = discord.Embed(title="🛒 CoinVault Shop", color=0xEB459E)
    e.description = "Spend your credits here!\n"
    for item, data in SHOP_ITEMS.items():
        e.add_field(name=f"`-buy {item}` — {data['cost']:,} credits", value=data['desc'], inline=False)
    e.set_footer(text="Credits are earned by chatting, daily, work, gambling, and selling coins")
    await ctx.send(embed=e)

@bot.command()
async def buy(ctx, item: str = None, *args):
    if item is None:
        await ctx.send("❌ Usage: `-buy <item>`. See `-shop` for items.")
        return

    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    item = item.lower()

    # ── Crates ──
    if item in ("crate", "crate_x3", "crate_x5"):
        count_map = {"crate": 1, "crate_x3": 3, "crate_x5": 5}
        cost_map  = {"crate": 100, "crate_x3": 270, "crate_x5": 420}
        n    = count_map[item]
        cost = cost_map[item]

        u = get_user(uid)
        if not u:
            await ctx.send("❌ Could not load your profile. Try again.")
            return
        if u['credits'] < cost:
            await ctx.send(f"❌ Need **{cost:,} credits**. You have **{u['credits']:,}**.")
            return

        bank_cut = max(1, int(cost * CRATE_FEE_PCT))
        conn = db()
        cur = conn.cursor()
        try:
            cur.execute("UPDATE users SET credits = credits - %s WHERE user_id = %s", (cost, uid))
            cur.execute("UPDATE bank SET total = total + %s WHERE id=1", (bank_cut,))
            cur.execute("INSERT INTO bank_log (source,amount) VALUES ('crate_fee',%s)", (bank_cut,))

            opened = []
            for _ in range(n):
                coin = generate_coin()
                # Insert without RETURNING, then fetch the id separately
                cur.execute("""
                    INSERT INTO coins (owner_id, material, variant, status, float, serial,
                                       base_value, mat_mult, var_mult, sta_mult, flt_mult,
                                       ser_mult, total_mult, value)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (uid, coin['material'], coin['variant'], coin['status'], coin['float'],
                      coin['serial'], coin['base_value'], coin['mat_mult'], coin['var_mult'],
                      coin['sta_mult'], coin['flt_mult'], coin['ser_mult'], coin['total_mult'],
                      coin['value']))
                
                # Get the last inserted id
                cur.execute("SELECT lastval() as id")
                coin['id'] = cur.fetchone()['id']
                opened.append(coin)

            sync_coin_count(uid, cur)
            conn.commit()
        except Exception as ex:
            conn.rollback()
            print(f"buy crate error: {ex}")
            await ctx.send(f"❌ An error occurred opening crates. Error: {str(ex)[:100]}")
            return
        finally:
            release(conn)

        credits_left = u['credits'] - cost
        if n == 1:
            coin = opened[0]
            serial_str = str(coin['serial']).zfill(4)
            tier = tier_emoji(coin['value'])
            e = discord.Embed(title=f"📦 Crate Opened! {tier}", color=0xFFD700)
            e.add_field(name="🪙 Coin",
                        value=f"**{coin['variant']} {coin['material']} Coin** `#{serial_str}`",
                        inline=False)
            e.add_field(name="📋 Attributes", value=(
                f"Material: **{coin['material']}** (×{coin['mat_mult']})\n"
                f"Variant: **{coin['variant']}** (×{coin['var_mult']})\n"
                f"Status: **{coin['status']}** (×{coin['sta_mult']})\n"
                f"Float: **{coin['float']}** (×{coin['flt_mult']})\n"
                f"Serial: **#{serial_str}** (×{coin['ser_mult']})"
            ), inline=True)
            e.add_field(name="💰 Value", value=(
                f"Base: **${coin['base_value']:.2f}**\n"
                f"Total ×: **{coin['total_mult']:.4f}**\n"
                f"**Final: ${coin['value']:.4f}**"
            ), inline=True)
            e.set_footer(text=f"Coin ID: #{coin['id']} • Credits left: {credits_left:,}")
            await ctx.send(embed=e)
        else:
            e = discord.Embed(title=f"📦 {n} Crates Opened!", color=0xFFD700)
            lines = []
            for c in opened:
                tier = tier_emoji(c['value'])
                lines.append(
                    f"{tier} `#{c['id']}` **{c['variant']} {c['material']}** "
                    f"#{str(c['serial']).zfill(4)} — **${c['value']:.4f}**"
                )
            e.description = "\n".join(lines)
            total_val = sum(c['value'] for c in opened)
            e.set_footer(text=f"Total value: ${total_val:.4f} • Credits left: {credits_left:,}")
            await ctx.send(embed=e)
        return

    # ── Polish ──
    if item == "polish":
        if not args:
            await ctx.send("❌ Usage: `-buy polish <coin_id>`")
            return
        try:
            coin_id = int(args[0])
        except ValueError:
            await ctx.send("❌ Invalid coin ID.")
            return

        cost = SHOP_ITEMS['polish']['cost']
        u = get_user(uid)
        if not u:
            await ctx.send("❌ Could not load your profile. Try again.")
            return
        if u['credits'] < cost:
            await ctx.send(f"❌ Need **{cost:,} credits**. You have **{u['credits']:,}**.")
            return

        conn = db()
        cur = conn.cursor()
        try:
            cur.execute("SELECT * FROM coins WHERE id = %s AND owner_id = %s", (coin_id, uid))
            c = cur.fetchone()
            if not c:
                await ctx.send(f"❌ Coin #{coin_id} not in your inventory.")
                return

            cur_status = c['status']
            if cur_status not in STATUS_ORDER:
                await ctx.send("❌ Can't polish this coin.")
                return
            idx = STATUS_ORDER.index(cur_status)
            if idx >= len(STATUS_ORDER) - 1:
                await ctx.send("❌ This coin is already at max status (**Stunning**).")
                return

            new_status   = STATUS_ORDER[idx + 1]
            new_sta_mult = next(m for n, m, _ in STATUSES if n == new_status)
            new_total    = round(c['mat_mult'] * c['var_mult'] * new_sta_mult * c['flt_mult'] * c['ser_mult'], 4)
            new_value    = round(c['base_value'] * new_total, 4)

            cur.execute("UPDATE users SET credits = credits - %s WHERE user_id = %s", (cost, uid))
            cur.execute(
                "UPDATE coins SET status=%s, sta_mult=%s, total_mult=%s, value=%s WHERE id=%s",
                (new_status, new_sta_mult, new_total, new_value, coin_id)
            )
            conn.commit()
        except Exception as ex:
            conn.rollback()
            print(f"buy polish error: {ex}")
            await ctx.send("❌ An error occurred. Try again.")
            return
        finally:
            release(conn)

        e = discord.Embed(title="✨ Coin Polished!", color=0x57F287)
        e.description = (
            f"Coin `#{coin_id}` upgraded: **{cur_status}** → **{new_status}**\n"
            f"New value: **${new_value:.4f}**"
        )
        await ctx.send(embed=e)
        return

    # ── Rename ──
    if item == "rename":
        if len(args) < 2:
            await ctx.send("❌ Usage: `-buy rename <coin_id> <new name>`")
            return
        try:
            coin_id = int(args[0])
        except ValueError:
            await ctx.send("❌ Invalid coin ID.")
            return
        new_name = " ".join(args[1:])[:40]

        cost = SHOP_ITEMS['rename']['cost']
        u = get_user(uid)
        if not u:
            await ctx.send("❌ Could not load your profile. Try again.")
            return
        if u['credits'] < cost:
            await ctx.send(f"❌ Need **{cost:,} credits**. You have **{u['credits']:,}**.")
            return

        conn = db()
        cur = conn.cursor()
        try:
            cur.execute("SELECT id FROM coins WHERE id = %s AND owner_id = %s", (coin_id, uid))
            if not cur.fetchone():
                await ctx.send(f"❌ Coin #{coin_id} not in your inventory.")
                return
            cur.execute("UPDATE users SET credits = credits - %s WHERE user_id = %s", (cost, uid))
            cur.execute("UPDATE coins SET custom_name = %s WHERE id = %s", (new_name, coin_id))
            conn.commit()
        except Exception as ex:
            conn.rollback()
            print(f"buy rename error: {ex}")
            await ctx.send("❌ An error occurred. Try again.")
            return
        finally:
            release(conn)

        await ctx.send(f"✅ Coin `#{coin_id}` renamed to **{new_name}**!")
        return

    await ctx.send(f"❌ Unknown item `{item}`. See `-shop`.")

@bot.command(aliases=['inv'])
async def inventory(ctx, page: int = 1):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    per_page = 8
    offset   = (page - 1) * per_page

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) as c FROM coins WHERE owner_id = %s", (uid,))
        total = cur.fetchone()['c']
        cur.execute(
            "SELECT * FROM coins WHERE owner_id = %s ORDER BY value DESC LIMIT %s OFFSET %s",
            (uid, per_page, offset)
        )
        coins = cur.fetchall()
    except Exception as ex:
        print(f"inventory error: {ex}")
        await ctx.send("❌ An error occurred loading inventory.")
        return
    finally:
        release(conn)

    if not coins:
        if page > 1:
            await ctx.send(f"❌ No coins on page {page}.")
        else:
            await ctx.send("🎒 Your inventory is empty! Use `-buy crate` to open one.")
        return

    pages = max(1, math.ceil(total / per_page))
    e = discord.Embed(title=f"🎒 {ctx.author.display_name}'s Inventory", color=0x5865F2)
    lines = []
    for c in coins:
        serial_str = str(c['serial']).zfill(4)
        name = c.get('custom_name') or f"{c['variant']} {c['material']} Coin"
        tier = tier_emoji(c['value'])
        lines.append(
            f"{tier} `#{c['id']}` **{name}** #{serial_str}\n"
            f"  {c['status']} | {c['float']} | **${c['value']:.4f}**"
        )
    e.description = f"Page **{page}/{pages}** | Total: **{total}** coins\n\n" + "\n\n".join(lines)
    e.set_footer(text=f"-coin <id> for details • -inventory {page+1} for next page")
    await ctx.send(embed=e)

@bot.command()
async def coin(ctx, coin_id: int):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT c.*, u.username FROM coins c JOIN users u ON u.user_id = c.owner_id WHERE c.id = %s
        """, (coin_id,))
        c = cur.fetchone()
    except Exception as ex:
        print(f"coin command error: {ex}")
        await ctx.send("❌ An error occurred. Try again.")
        return
    finally:
        release(conn)

    if not c:
        await ctx.send(f"❌ Coin #{coin_id} not found.")
        return

    serial_str = str(c['serial']).zfill(4)
    tier = tier_emoji(c['value'])
    display_name = c.get('custom_name') or f"{c['variant']} {c['material']} Coin"

    e = discord.Embed(title=f"{tier} Coin #{coin_id} — {display_name}", color=0xFFD700)
    e.add_field(name="Owner",    value=c['username'],   inline=True)
    e.add_field(name="Serial",   value=f"#{serial_str}", inline=True)
    obtained = c['obtained_at']
    e.add_field(name="Obtained", value=obtained.strftime("%Y-%m-%d") if obtained else "Unknown", inline=True)
    e.add_field(name="📊 Attributes", value=(
        f"Material: **{c['material']}** (×{c['mat_mult']})\n"
        f"Variant: **{c['variant']}** (×{c['var_mult']})\n"
        f"Status: **{c['status']}** (×{c['sta_mult']})\n"
        f"Float: **{c['float']}** (×{c['flt_mult']})\n"
        f"Serial #{serial_str}: (×{c['ser_mult']})"
    ), inline=True)
    e.add_field(name="💰 Valuation", value=(
        f"Base: **${c['base_value']:.2f}**\n"
        f"Combined ×: **{c['total_mult']:.4f}**\n"
        f"**Value: ${c['value']:.4f}**"
    ), inline=True)
    await ctx.send(embed=e)

@bot.command()
async def sell(ctx, coin_id: int):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM coins WHERE id = %s AND owner_id = %s", (coin_id, uid))
        c = cur.fetchone()
        if not c:
            await ctx.send(f"❌ Coin #{coin_id} not found in your inventory.")
            return

        cur.execute("SELECT id FROM auctions WHERE coin_id = %s AND status = 'active'", (coin_id,))
        if cur.fetchone():
            await ctx.send(f"❌ Coin #{coin_id} is in an active auction. Cancel it first.")
            return

        credits_earned = coin_value_to_credits(c['value'])
        cur.execute("DELETE FROM coins WHERE id = %s", (coin_id,))
        cur.execute("UPDATE users SET credits = credits + %s WHERE user_id = %s", (credits_earned, uid))
        cur.execute("INSERT INTO credit_log (user_id, amount, reason) VALUES (%s,%s,'sell_coin')", (uid, credits_earned))
        sync_coin_count(uid, cur)
        conn.commit()
    except Exception as ex:
        conn.rollback()
        print(f"sell error: {ex}")
        await ctx.send("❌ An error occurred. Try again.")
        return
    finally:
        release(conn)

    name = c.get('custom_name') or f"{c['variant']} {c['material']} Coin"
    e = discord.Embed(title="💸 Coin Sold!", color=0x57F287)
    e.description = (
        f"Sold **{name}** `#{str(c['serial']).zfill(4)}`\n"
        f"Value: **${c['value']:.4f}** → **{credits_earned:,} credits**"
    )
    await ctx.send(embed=e)

@bot.command()
async def sellall(ctx):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT c.* FROM coins c
            WHERE c.owner_id = %s
            AND c.id NOT IN (SELECT coin_id FROM auctions WHERE status = 'active')
        """, (uid,))
        coins = cur.fetchall()

        if not coins:
            await ctx.send("🎒 No sellable coins (coins in active auctions are excluded).")
            return

        total_credits = sum(coin_value_to_credits(c['value']) for c in coins)
        ids = [c['id'] for c in coins]

        cur.execute("DELETE FROM coins WHERE id = ANY(%s)", (ids,))
        cur.execute("UPDATE users SET credits = credits + %s WHERE user_id = %s", (total_credits, uid))
        cur.execute("INSERT INTO credit_log (user_id, amount, reason) VALUES (%s,%s,'sellall')", (uid, total_credits))
        sync_coin_count(uid, cur)
        conn.commit()
    except Exception as ex:
        conn.rollback()
        print(f"sellall error: {ex}")
        await ctx.send("❌ An error occurred. Try again.")
        return
    finally:
        release(conn)

    e = discord.Embed(title="💸 Sold All Coins!", color=0x57F287)
    e.description = f"Sold **{len(coins)}** coin(s) for **{total_credits:,} credits** total."
    await ctx.send(embed=e)

@bot.command()
async def trade(ctx, member: discord.Member, *, args: str = ""):
    if member.bot or member.id == ctx.author.id:
        await ctx.send("❌ Invalid trade target.")
        return

    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    ensure_user(member.id, str(member))

    coin_ids      = []
    credits_offer = 0
    parts = args.strip().split()

    for part in parts:
        if part.lower().startswith("credits:"):
            try:
                credits_offer = int(part.split(":")[1])
            except Exception:
                await ctx.send("❌ Invalid credits format. Use `credits:500`")
                return
        elif part:
            try:
                coin_ids = [int(x.strip()) for x in part.split(",") if x.strip()]
            except Exception:
                await ctx.send("❌ Invalid coin IDs.")
                return

    if not coin_ids and credits_offer == 0:
        await ctx.send("❌ Specify coin IDs and/or a credits offer. E.g.: `-trade @Bob 12,15 credits:500`")
        return

    if coin_ids:
        conn = db()
        cur = conn.cursor()
        try:
            cur.execute("SELECT id FROM coins WHERE id = ANY(%s) AND owner_id = %s", (coin_ids, uid))
            found = [r['id'] for r in cur.fetchall()]
        finally:
            release(conn)
        invalid = set(coin_ids) - set(found)
        if invalid:
            await ctx.send(f"❌ Coins `{invalid}` not in your inventory.")
            return

    if credits_offer > 0:
        u = get_user(uid)
        if not u or u['credits'] < credits_offer:
            bal = u['credits'] if u else 0
            await ctx.send(f"❌ Insufficient credits. You have **{bal:,}**.")
            return

    conn = db()
    cur = conn.cursor()
    try:
        ids_str = ",".join(str(x) for x in coin_ids)
        cur.execute("""
            INSERT INTO trades (initiator_id, receiver_id, coin_ids, credits_offer)
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (uid, member.id, ids_str, credits_offer))
        trade_id = cur.fetchone()['id']
        conn.commit()
    except Exception as ex:
        conn.rollback()
        print(f"trade error: {ex}")
        await ctx.send("❌ An error occurred creating trade. Try again.")
        return
    finally:
        release(conn)

    e = discord.Embed(title=f"🤝 Trade Offer #{trade_id}", color=0xFEE75C)
    e.description = f"**{ctx.author.display_name}** → **{member.display_name}**"
    lines = []
    if coin_ids:
        lines.append(f"Coins: `{', '.join('#'+str(i) for i in coin_ids)}`")
    if credits_offer > 0:
        tax = int(round(credits_offer * TRADE_TAX_PCT))
        net = credits_offer - tax
        lines.append(f"Credits: **{credits_offer:,}** (receiver gets **{net:,}** after {int(TRADE_TAX_PCT*100)}% tax)")
    e.add_field(name="📤 Offer", value="\n".join(lines) or "None", inline=False)
    e.set_footer(text="Expires in 2 minutes")

    view = TradeView(trade_id, uid, member.id)
    await ctx.send(f"{member.mention}", embed=e, view=view)

@bot.command()
async def trades(ctx):
    uid = ctx.author.id
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT * FROM trades
            WHERE (initiator_id = %s OR receiver_id = %s) AND status = 'pending'
            ORDER BY created_at DESC LIMIT 10
        """, (uid, uid))
        rows = cur.fetchall()
    finally:
        release(conn)

    if not rows:
        await ctx.send("📭 No pending trades.")
        return

    e = discord.Embed(title="📋 Pending Trades", color=0xFEE75C)
    for t in rows:
        role = "Sender" if t['initiator_id'] == uid else "Receiver"
        e.add_field(
            name=f"Trade #{t['id']} [{role}]",
            value=(
                f"Coins: `{t['coin_ids'] or 'none'}` | Credits: {t['credits_offer']:,}\n"
                f"Created: {t['created_at'].strftime('%Y-%m-%d %H:%M')}"
            ),
            inline=False
        )
    await ctx.send(embed=e)

@bot.command()
async def auction(ctx, coin_id: int, start_price: int, hours: float = 24.0):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))

    if start_price <= 0:
        await ctx.send("❌ Start price must be > 0 credits.")
        return
    if hours < 1 or hours > 168:
        await ctx.send("❌ Duration must be 1–168 hours.")
        return

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM coins WHERE id = %s AND owner_id = %s", (coin_id, uid))
        c = cur.fetchone()
        if not c:
            await ctx.send(f"❌ Coin #{coin_id} not in your inventory.")
            return

        cur.execute("SELECT id FROM auctions WHERE coin_id = %s AND status = 'active'", (coin_id,))
        if cur.fetchone():
            await ctx.send(f"❌ Coin #{coin_id} is already listed.")
            return

        ends_at = datetime.now(timezone.utc) + timedelta(hours=hours)
        cur.execute("""
            INSERT INTO auctions (seller_id, coin_id, start_price, ends_at)
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (uid, coin_id, start_price, ends_at))
        auction_id = cur.fetchone()['id']
        conn.commit()
    except Exception as ex:
        conn.rollback()
        print(f"auction error: {ex}")
        await ctx.send("❌ An error occurred. Try again.")
        return
    finally:
        release(conn)

    name = c.get('custom_name') or f"{c['variant']} {c['material']} Coin"
    e = discord.Embed(title="🏪 Auction Listed!", color=0x57F287)
    e.description = coin_display(c)
    e.add_field(name="Starting Price", value=f"**{start_price:,} credits**", inline=True)
    e.add_field(name="Ends At",        value=f"<t:{int(ends_at.timestamp())}:R>", inline=True)
    e.add_field(name="Fee",            value=f"{int(MARKET_FEE_PCT*100)}% on final sale", inline=True)
    e.set_footer(text=f"Auction ID: #{auction_id}")
    await ctx.send(embed=e)

@bot.command()
async def market(ctx, page: int = 1):
    per_page = 5
    offset   = (page - 1) * per_page

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) as c FROM auctions WHERE status = 'active'")
        total_row = cur.fetchone()
        total = total_row['c'] if total_row else 0
        
        if total == 0:
            await ctx.send("🏪 No active auctions. List yours with `-auction <coin_id> <price>`.")
            return
            
        cur.execute("""
            SELECT a.*, c.material, c.variant, c.status as cond, c.float, c.serial,
                   c.value as coin_val, c.custom_name, u.username as seller_name
            FROM auctions a
            JOIN coins c ON c.id = a.coin_id
            JOIN users u ON u.user_id = a.seller_id
            WHERE a.status = 'active'
            ORDER BY a.ends_at ASC
            LIMIT %s OFFSET %s
        """, (per_page, offset))
        rows = cur.fetchall()
    except Exception as ex:
        print(f"market error: {ex}")
        await ctx.send("❌ An error occurred loading the market. Please try again later.")
        return
    finally:
        release(conn)

    if not rows:
        if page > 1:
            await ctx.send(f"❌ No auctions on page {page}.")
        else:
            await ctx.send("🏪 No active auctions. List yours with `-auction <coin_id> <price>`.")
        return

    pages = max(1, math.ceil(total / per_page))
    e = discord.Embed(title=f"🏪 Coin Marketplace — Page {page}/{pages}", color=0xEB459E)
    e.description = f"**{total}** active listing(s)\n"

    for a in rows:
        serial_str = str(a['serial']).zfill(4)
        tier = tier_emoji(a['coin_val'])
        top_bid = f"{a['current_bid']:,} credits" if a['current_bid'] else "No bids"
        name = a.get('custom_name') or f"{a['variant']} {a['material']} Coin"
        ends_ts = a['ends_at']
        if ends_ts.tzinfo is None:
            ends_ts = ends_ts.replace(tzinfo=timezone.utc)
        e.add_field(
            name=f"{tier} Auction #{a['id']} — {name} #{serial_str}",
            value=(
                f"Cond: **{a['cond']}** | Float: **{a['float']}** | Coin Value: **${a['coin_val']:.4f}**\n"
                f"Start: **{a['start_price']:,}** | Top Bid: **{top_bid}**\n"
                f"Seller: {a['seller_name']} | Ends: <t:{int(ends_ts.timestamp())}:R>"
            ),
            inline=False
        )

    e.set_footer(text="Use -bid <auction_id> to place a bid")
    await ctx.send(embed=e)

@bot.command()
async def bid(ctx, auction_id: int):
    ensure_user(ctx.author.id, str(ctx.author))
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM auctions WHERE id = %s AND status = 'active'", (auction_id,))
        a = cur.fetchone()
    finally:
        release(conn)

    if not a:
        await ctx.send(f"❌ Auction #{auction_id} not found or ended.")
        return

    min_bid = max(a['start_price'], (a['current_bid'] or 0) + 1)
    view = AuctionView(auction_id)
    await ctx.send(f"💰 Bidding on Auction **#{auction_id}** | Min bid: **{min_bid:,} credits**", view=view)

@bot.command()
async def myauctions(ctx):
    uid = ctx.author.id
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT a.*, c.material, c.variant, c.serial, c.custom_name
            FROM auctions a JOIN coins c ON c.id = a.coin_id
            WHERE a.seller_id = %s AND a.status = 'active'
            ORDER BY a.ends_at ASC
        """, (uid,))
        rows = cur.fetchall()
    finally:
        release(conn)

    if not rows:
        await ctx.send("📭 You have no active listings.")
        return

    e = discord.Embed(title="📋 Your Active Auctions", color=0xEB459E)
    for a in rows:
        serial_str = str(a['serial']).zfill(4)
        top_bid = f"{a['current_bid']:,} credits" if a['current_bid'] else "No bids"
        name = a.get('custom_name') or f"{a['variant']} {a['material']} Coin"
        ends_ts = a['ends_at']
        if ends_ts.tzinfo is None:
            ends_ts = ends_ts.replace(tzinfo=timezone.utc)
        e.add_field(
            name=f"Auction #{a['id']} — {name} #{serial_str}",
            value=(
                f"Start: {a['start_price']:,} | Top Bid: {top_bid}\n"
                f"Ends: <t:{int(ends_ts.timestamp())}:R>"
            ),
            inline=False
        )
    await ctx.send(embed=e)

@bot.command()
async def cancelauction(ctx, auction_id: int):
    uid = ctx.author.id
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT * FROM auctions WHERE id = %s AND seller_id = %s AND status = 'active'",
            (auction_id, uid)
        )
        a = cur.fetchone()
        if not a:
            await ctx.send(f"❌ Auction #{auction_id} not found or not yours.")
            return

        if a['bidder_id'] and a['current_bid']:
            cur.execute(
                "UPDATE users SET credits = credits + %s WHERE user_id = %s",
                (a['current_bid'], a['bidder_id'])
            )

        cur.execute("UPDATE coins SET owner_id = %s WHERE id = %s", (uid, a['coin_id']))
        cur.execute("UPDATE auctions SET status = 'cancelled' WHERE id = %s", (auction_id,))
        conn.commit()
    except Exception as ex:
        conn.rollback()
        print(f"cancelauction error: {ex}")
        await ctx.send("❌ An error occurred. Try again.")
        return
    finally:
        release(conn)

    await ctx.send(f"✅ Auction **#{auction_id}** cancelled. Coin returned to your inventory.")

@bot.command()
async def profile(ctx, member: discord.Member = None):
    target = member or ctx.author
    uid = target.id
    ensure_user(uid, str(target))

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM users WHERE user_id = %s", (uid,))
        u = cur.fetchone()
        cur.execute("SELECT value FROM coins WHERE owner_id = %s ORDER BY value DESC LIMIT 1", (uid,))
        best = cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(value),0) as total FROM coins WHERE owner_id = %s", (uid,))
        total_val = float(cur.fetchone()['total'])
        cur.execute("SELECT COUNT(*) as c FROM auctions WHERE seller_id = %s AND status = 'sold'", (uid,))
        sales = cur.fetchone()['c']
        cur.execute(
            "SELECT COUNT(*) as c FROM trades WHERE (initiator_id=%s OR receiver_id=%s) AND status='completed'",
            (uid, uid)
        )
        trades_done = cur.fetchone()['c']
    except Exception as ex:
        print(f"profile error: {ex}")
        await ctx.send("❌ An error occurred loading profile.")
        return
    finally:
        release(conn)

    if not u:
        await ctx.send("❌ User not found.")
        return

    prestige_val = u.get('prestige') or 0
    pmult = prestige_multiplier(prestige_val)
    e = discord.Embed(title=f"👤 {target.display_name}'s Profile", color=0x5865F2)
    e.set_thumbnail(url=target.display_avatar.url)
    e.add_field(name="🎟️ Credits",       value=f"{u['credits']:,}",             inline=True)
    e.add_field(name="🪙 Coins",          value=str(u['total_coins']),            inline=True)
    e.add_field(name="⭐ Prestige",       value=f"{prestige_val} (×{pmult:.1f})", inline=True)
    e.add_field(name="📈 Portfolio",      value=f"${total_val:.4f}",              inline=True)
    e.add_field(name="🏆 Best Coin",      value=f"${best['value']:.4f}" if best else "None", inline=True)
    e.add_field(name="🛒 Sales / Trades", value=f"{sales} / {trades_done}",       inline=True)
    e.add_field(name="🔥 Daily Streak",   value=f"{u.get('daily_streak', 0)} days", inline=True)
    joined = u.get('joined_at')
    e.set_footer(text=f"Member since {joined.strftime('%Y-%m-%d') if joined else 'Unknown'}")
    await ctx.send(embed=e)

@bot.command(aliases=['lb'])
async def leaderboard(ctx):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT u.username, u.credits, u.total_coins, u.prestige,
                   COALESCE(SUM(c.value), 0) as portfolio
            FROM users u
            LEFT JOIN coins c ON c.owner_id = u.user_id
            GROUP BY u.user_id, u.username, u.credits, u.total_coins, u.prestige
            ORDER BY portfolio DESC
            LIMIT 10
        """)
        rows = cur.fetchall()
    except Exception as ex:
        print(f"leaderboard error: {ex}")
        await ctx.send("❌ An error occurred. Try again.")
        return
    finally:
        release(conn)

    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    e = discord.Embed(title="🏆 CoinVault — Top Collectors (by Portfolio)", color=0xFFD700)
    for i, r in enumerate(rows):
        prestige_val = r.get('prestige') or 0
        star = f"⭐×{prestige_val}" if prestige_val else ""
        e.add_field(
            name=f"{medals[i]} {r['username']} {star}",
            value=f"Portfolio: **${float(r['portfolio']):.4f}** | Credits: **{r['credits']:,}** | Coins: **{r['total_coins']}**",
            inline=False
        )
    if not rows:
        e.description = "No users yet!"
    await ctx.send(embed=e)

@bot.command()
async def richlist(ctx):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT username, credits, prestige FROM users ORDER BY credits DESC LIMIT 10")
        rows = cur.fetchall()
    finally:
        release(conn)

    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    e = discord.Embed(title="💰 CoinVault — Credit Rich List", color=0x57F287)
    for i, r in enumerate(rows):
        prestige_val = r.get('prestige') or 0
        star = f" ⭐×{prestige_val}" if prestige_val else ""
        e.add_field(
            name=f"{medals[i]} {r['username']}{star}",
            value=f"**{r['credits']:,} credits**",
            inline=False
        )
    if not rows:
        e.description = "No users yet!"
    await ctx.send(embed=e)

@bot.command()
async def bank(ctx):
    total = get_bank()
    n     = count_users()
    share = total // n if n > 0 else 0

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM daily_log ORDER BY paid_date DESC LIMIT 1")
        last = cur.fetchone()
        cur.execute(
            "SELECT COALESCE(SUM(amount),0) as t FROM bank_log WHERE logged_at > NOW() - INTERVAL '24 hours'"
        )
        day_income = cur.fetchone()['t']
    finally:
        release(conn)

    e = discord.Embed(title="🏦 CoinVault Bank Treasury", color=0x57F287)
    e.add_field(name="💰 Balance",         value=f"**{total:,} credits**",      inline=True)
    e.add_field(name="👥 Users",           value=str(n),                         inline=True)
    e.add_field(name="📤 Projected Share", value=f"**{share:,} credits/user**",  inline=True)
    e.add_field(name="📈 Inflow (24h)",    value=f"{day_income:,} credits",       inline=True)
    if last:
        e.add_field(
            name="📅 Last Payout",
            value=f"{last['paid_date']} — {last['amount']:,}/user",
            inline=True
        )
    e.set_footer(text="Funded by: crate fees • trade taxes • gambling house edge • rob fines • prestige")
    await ctx.send(embed=e)

@bot.command()
async def stats(ctx):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM users WHERE user_id = %s", (uid,))
        u = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) as c, COALESCE(SUM(value),0) as s FROM coins WHERE owner_id = %s",
            (uid,)
        )
        cs = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) as c FROM trades WHERE (initiator_id=%s OR receiver_id=%s) AND status='completed'",
            (uid, uid)
        )
        trades_done = cur.fetchone()['c']
        cur.execute(
            "SELECT COALESCE(SUM(amount),0) as t FROM credit_log WHERE user_id=%s AND amount > 0",
            (uid,)
        )
        total_earned = cur.fetchone()['t']
        cur.execute(
            "SELECT material, COUNT(*) as c FROM coins WHERE owner_id=%s GROUP BY material ORDER BY c DESC LIMIT 3",
            (uid,)
        )
        top_mats = cur.fetchall()
    except Exception as ex:
        print(f"stats error: {ex}")
        await ctx.send("❌ An error occurred loading stats.")
        return
    finally:
        release(conn)

    if not u:
        await ctx.send("❌ User not found.")
        return

    prestige_val = u.get('prestige') or 0
    pmult = prestige_multiplier(prestige_val)
    e = discord.Embed(title=f"📊 Stats — {ctx.author.display_name}", color=0x5865F2)
    e.add_field(name="🎟️ Credits",        value=f"{u['credits']:,}",          inline=True)
    e.add_field(name="💹 Total Earned",    value=f"{total_earned:,} credits",   inline=True)
    e.add_field(name="⭐ Prestige",        value=f"{prestige_val} (×{pmult:.1f})", inline=True)
    e.add_field(name="🪙 Coins Owned",     value=str(cs['c'] or 0),             inline=True)
    e.add_field(name="📈 Portfolio Value", value=f"${float(cs['s']):.4f}",       inline=True)
    e.add_field(name="🤝 Trades Done",     value=str(trades_done),              inline=True)
    e.add_field(name="🔥 Daily Streak",    value=f"{u.get('daily_streak', 0)} days", inline=True)
    if top_mats:
        e.add_field(
            name="🏅 Top Materials",
            value="\n".join(f"{r['material']}: {r['c']}" for r in top_mats),
            inline=True
        )
    await ctx.send(embed=e)

# ─── Run ──────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
