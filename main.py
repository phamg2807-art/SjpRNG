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
CRATE_FEE_PCT      = 0.10
TRADE_TAX_PCT      = 0.05
MARKET_FEE_PCT     = 0.08
CREDITS_PER_MSG    = 1
MSG_COOLDOWN_S     = 30
DAILY_CREDITS      = 50
DAILY_STREAK_BONUS = 5
WORK_COOLDOWN_H    = 0.5
WORK_BASE          = 20
WORK_MULTIPLIER    = 1.1
ROB_COOLDOWN_H     = 6
ROB_SUCCESS_PCT    = 0.50          # upgraded from 0.40
ROB_MAX_STEAL_PCT  = 0.20
ROB_FINE_PCT       = 0.15
GAMBLE_MIN         = 10
PRESTIGE_COST      = 5000
BANK_INTEREST_PCT  = 0.05
BANK_INTEREST_MINS = 10

# ─── Economy Growth Constants ─────────────────────────────────────────────────
SUPPLY_DECAY_MINS      = 60
SUPPLY_DECAY_RATE      = 0.85
DEMAND_IMPACT          = 0.15
INFLATION_RATE         = 0.0002
MAX_CRATE_COST         = 250
ECONOMY_SINK_SELL_PCT  = 0.05
BOOM_CHANCE            = 0.08
BUST_CHANCE            = 0.05
BOOM_MULT              = 2.5
BUST_MULT              = 0.4
EVENT_DURATION_MINS    = 40

# ─── Black Market ─────────────────────────────────────────────────────────────
BLACK_MARKET_CHANNEL_ID = 1514515923455705178
BLACK_MARKET_CHANCE     = 0.10   # 10% per 2h check
BLACK_MARKET_DURATION_H = 2      # market stays open for 2 hours

# ─── Hacking Constants ────────────────────────────────────────────────────────
HACK_BASE_CHANCE    = 0.01   # 1% base without Data Leak Generator
HACK_MAX_CHANCE     = 0.51   # 51% max
HACK_MIN_EARN       = 2000
HACK_MAX_EARN       = 25000
HACK_PENALTY_PCT    = 0.10   # victim gains 10% of hacker's bank on failed hack
TRANSFER_DELAY_MINS = 30

SHOP_ITEMS = {
    "rename":        {"cost": 200,  "desc": "Rename one of your coins (cosmetic only)"},
    "polish":        {"cost": 150,  "desc": "Upgrade a coin's Status by one tier"},
    "crate":         {"cost": 100,  "desc": "Open a coin crate"},
    "crate_x3":      {"cost": 270,  "desc": "Open 3 crates at once (10% discount)"},
    "crate_x5":      {"cost": 420,  "desc": "Open 5 crates at once (16% discount)"},
    "float_changer": {"cost": 320,  "desc": "Re-randomize a coin's float attribute"},
    "market_trigger":{"cost": 250,  "desc": "Trigger a random market boom or bust event"},
    "workshop":      {"cost": 2000, "desc": "Crafting Workshop — required for crafting items"},
    "cognitive_machine": {"cost": 6000,  "desc": "Auto-work every 30min at 0.75× salary (toggle with -work)"},
    "ai_machine":        {"cost": 50000, "desc": "Auto-work every 15min at 2× salary (toggle with -work)"},
}

# ─── Admin ─────────────────────────────────────────────────────────────────────
ADMIN_ID = 920309927375933490

# ─── Job Rank System ──────────────────────────────────────────────────────────
JOB_RANKS = [
    ("Intern",          0,    20,   "🟤"),
    ("Junior Worker",   5,    35,   "⚪"),
    ("Worker",          15,   55,   "🟡"),
    ("Senior Worker",   30,   80,   "🔵"),
    ("Specialist",      55,   120,  "🟢"),
    ("Expert",          90,   175,  "🟠"),
    ("Lead",            140,  250,  "🔴"),
    ("Manager",         200,  350,  "💜"),
    ("Director",        300,  500,  "💎"),
    ("Executive",       450,  750,  "👑"),
    ("Vault Master",    650,  1200, "🌟"),
    ("Coin Legend",     900,  2000, "🌌"),
]

JOB_TITLES = {
    "Intern":         "Coin Sorting Intern",
    "Junior Worker":  "Mint Floor Assistant",
    "Worker":         "Vault Clerk",
    "Senior Worker":  "Coin Authenticator",
    "Specialist":     "Market Analyst",
    "Expert":         "Senior Appraiser",
    "Lead":           "Lead Vault Technician",
    "Manager":        "Treasury Manager",
    "Director":       "Director of Acquisitions",
    "Executive":      "Chief Coin Officer",
    "Vault Master":   "Vault Master",
    "Coin Legend":    "Legendary Coin Baron",
}

def get_job_rank(work_count: int) -> tuple:
    current = JOB_RANKS[0]
    idx = 0
    for i, (name, min_wc, salary, emoji) in enumerate(JOB_RANKS):
        if work_count >= min_wc:
            current = (name, min_wc, salary, emoji)
            idx = i
        else:
            break
    return current[0], current[2], current[3], idx

def get_next_job_rank(work_count: int):
    _, _, _, idx = get_job_rank(work_count)
    if idx + 1 < len(JOB_RANKS):
        nxt = JOB_RANKS[idx + 1]
        return nxt[0], nxt[1], nxt[2], nxt[3]
    return None

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
    ("Bad",          0.50,   15),
    ("Good",         1.00,    2),
    ("Great",        2.00,    4),
    ("Amazing",      3.00,    8),
    ("Heavenly",    15.00,   50),
    ("Godlike",     30.00,  100),
    ("Ascendant",   50.00,  250),
    ("Harmonious",  75.00,  350),
    ("Transcendence",100.00,500),
    ("Euphonious", 125.00,  600),
    ("Symphonious",150.00,  750),
    ("Euphoric",   175.00,  850),
    ("Dimensional",200.00, 1000),
    ("Illusional", 250.00, 2000),
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
    "negotiated a bulk coin deal",
    "inspected a rare coin collection",
    "managed the vault security systems",
    "oversaw the quarterly coin audit",
    "closed a major acquisition deal",
]

# ─── Market Price Fluctuation ─────────────────────────────────────────────────
MATERIAL_PRICE_RANGES = {
    "Plastic":  (0.4,   0.9),
    "Wood":     (0.6,   0.9),
    "Stone":    (0.8,   1.5),
    "Bronze":   (1.0,   2.0),
    "Copper":   (1.1,   2.3),
    "Iron":     (1.3,   2.8),
    "Steel":    (1.5,   3.0),
    "Gold":     (2.5,   5.0),
    "Aluminum": (1.5,   4.0),
    "Carbon":   (1.5,   3.0),
    "Tungsten": (2.0,   5.0),
    "Obsidian": (2.3,   6.0),
    "Topaz":    (2.1,   5.5),
    "Diamond":  (5.0,  10.0),
    "Amethyst": (10.0, 30.0),
    "Plasma":   (100.0,250.0),
}

FLOAT_PRICE_RANGES = {
    "Bad":          (0.3,   1.0),
    "Good":         (0.7,   1.3),
    "Great":        (1.6,   2.4),
    "Amazing":      (2.5,   3.5),
    "Heavenly":    (10.0,  30.0),
    "Godlike":     (25.0,  60.0),
    "Ascendant":   (35.0,  70.0),
    "Harmonious":  (60.0,  85.0),
    "Transcendence":(70.0,115.0),
    "Euphonious":  (110.0,145.0),
    "Symphonious": (135.0,165.0),
    "Euphoric":    (160.0,195.0),
    "Dimensional": (190.0,250.0),
    "Illusional":  (230.0,275.0),
}

STATUS_PRICE_RANGES = {
    "Broken":   (0.4,  0.6),
    "Crushed":  (0.5,  0.7),
    "Oxidized": (0.5,  1.0),
    "Scratched":(0.7,  1.2),
    "Old":      (0.8,  2.0),
    "Like New": (0.8,  1.2),
    "Normal":   (1.0,  2.0),
    "New":      (1.25, 3.0),
    "Sleek":    (1.5,  4.0),
    "Shiny":    (1.75, 5.0),
    "Modern":   (2.0,  6.0),
    "Elegant":  (2.5,  8.0),
    "Stunning": (2.75, 10.0),
}

# ─── Economy State (in-memory) ────────────────────────────────────────────────
_market_prices = {
    "materials": {},
    "floats":    {},
    "statuses":  {},
    "last_updated": None,
}

_supply_counters   = {mat: 0.0 for mat in MATERIAL_PRICE_RANGES}
_supply_last_decay = time.monotonic()
_market_events     = {}
_dynamic_crate_cost = CRATE_COST

# Black market state
_black_market_active  = False
_black_market_expires = None
_black_market_stock   = {}   # item_key -> remaining stock

# ─── Supply & Demand ──────────────────────────────────────────────────────────
def record_material_sale(material: str, quantity: int = 1):
    if material in _supply_counters:
        _supply_counters[material] += quantity

def decay_supply_counters():
    global _supply_last_decay
    now = time.monotonic()
    elapsed_mins = (now - _supply_last_decay) / 60
    ticks = int(elapsed_mins // SUPPLY_DECAY_MINS)
    if ticks > 0:
        for mat in _supply_counters:
            _supply_counters[mat] *= (SUPPLY_DECAY_RATE ** ticks)
        _supply_last_decay = now

def get_supply_demand_mult(material: str) -> float:
    decay_supply_counters()
    sales = _supply_counters.get(material, 0.0)
    normalised = min(sales / 20.0, 1.0)
    return round(1.0 + DEMAND_IMPACT - (normalised * DEMAND_IMPACT * 2), 4)

# ─── Boom/Bust Events ─────────────────────────────────────────────────────────
def maybe_trigger_market_event():
    now = datetime.now(timezone.utc)
    expired = [m for m, ev in _market_events.items() if now >= ev["expires"]]
    for m in expired:
        del _market_events[m]

    materials = list(MATERIAL_PRICE_RANGES.keys())

    if random.random() < BOOM_CHANCE:
        available = [m for m in materials if m not in _market_events]
        if available:
            mat  = random.choice(available)
            mult = round(random.uniform(1.5, BOOM_MULT), 2)
            _market_events[mat] = {
                "type": "boom",
                "mult": mult,
                "expires": now + timedelta(minutes=EVENT_DURATION_MINS),
            }
            print(f"📈 MARKET BOOM: {mat} ×{mult} for {EVENT_DURATION_MINS} mins")
            return ("boom", mat, mult)

    if random.random() < BUST_CHANCE:
        available = [m for m in materials if m not in _market_events]
        if available:
            mat  = random.choice(available)
            mult = round(random.uniform(BUST_MULT, 0.7), 2)
            _market_events[mat] = {
                "type": "bust",
                "mult": mult,
                "expires": now + timedelta(minutes=EVENT_DURATION_MINS),
            }
            print(f"📉 MARKET BUST: {mat} ×{mult} for {EVENT_DURATION_MINS} mins")
            return ("bust", mat, mult)

    return None

def get_event_mult(material: str) -> float:
    now = datetime.now(timezone.utc)
    ev = _market_events.get(material)
    if ev and now < ev["expires"]:
        return ev["mult"]
    return 1.0

# ─── Dynamic Crate Cost ───────────────────────────────────────────────────────
def get_dynamic_crate_cost() -> int:
    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) as c FROM users")
        users = cur.fetchone()['c'] or 1
        cur.execute("SELECT COUNT(*) as c FROM coins")
        total_coins = cur.fetchone()['c'] or 0
    except Exception:
        return CRATE_COST
    finally:
        release(conn)

    inflation_factor = 1.0 + (total_coins / 500) * INFLATION_RATE * 100
    cost = int(CRATE_COST * inflation_factor)
    return min(cost, MAX_CRATE_COST)

# ─── Market Price Generation ──────────────────────────────────────────────────
def generate_market_prices():
    event = maybe_trigger_market_event()

    mat = {}
    for name, (lo, hi) in MATERIAL_PRICE_RANGES.items():
        base    = random.uniform(lo, hi)
        sd_mult = get_supply_demand_mult(name)
        ev_mult = get_event_mult(name)
        raw     = base * sd_mult * ev_mult
        mat[name] = round(max(lo * 0.5, min(raw, hi * 3.0)), 4)

    flt = {}
    for name, (lo, hi) in FLOAT_PRICE_RANGES.items():
        flt[name] = round(random.uniform(lo, hi), 4)

    sta = {}
    for name, (lo, hi) in STATUS_PRICE_RANGES.items():
        sta[name] = round(random.uniform(lo, hi), 4)

    _market_prices["materials"]   = mat
    _market_prices["floats"]      = flt
    _market_prices["statuses"]    = sta
    _market_prices["last_updated"] = datetime.now(timezone.utc)
    print(f"✅ Market prices updated at {_market_prices['last_updated'].strftime('%H:%M UTC')}")
    return event

def get_status_market_mult(coin_row) -> float:
    status    = coin_row.get("status", "Normal")
    base_mult = _market_prices["statuses"].get(status, 1.0)
    if status == "Old":
        obtained = coin_row.get("obtained_at")
        if obtained:
            if obtained.tzinfo is None:
                obtained = obtained.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - obtained).days
            if age_days >= 30:
                base_mult = round(random.uniform(2.0, 30.0), 4)
    return base_mult

def get_market_price(coin_row) -> float:
    mat_mult = _market_prices["materials"].get(coin_row["material"], coin_row["mat_mult"])
    flt_mult = _market_prices["floats"].get(coin_row["float"],       coin_row["flt_mult"])
    sta_mult = get_status_market_mult(coin_row)
    total    = mat_mult * coin_row["var_mult"] * sta_mult * flt_mult * coin_row["ser_mult"]
    return round(coin_row["base_value"] * total, 4)

def weighted_choice(table):
    weights = [1.0 / d for _, _, d in table]
    total   = sum(weights)
    r       = random.uniform(0, total)
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
    if "99"  in str(s):          return s, 10.0
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

def coin_rarity_score(coin: dict) -> float:
    return round(
        coin.get("mat_mult", 1) +
        coin.get("var_mult", 1) +
        coin.get("sta_mult", 1) +
        coin.get("flt_mult", 1) +
        coin.get("ser_mult", 1),
        4
    )

def rarity_label(score: float) -> str:
    if score >= 500:  return "🌌 Transcendent"
    if score >= 200:  return "🌠 Mythic"
    if score >= 50:   return "👑 Legendary"
    if score >= 20:   return "💎 Epic"
    if score >= 10:   return "🔥 Rare"
    if score >= 5:    return "⭐ Uncommon"
    return "⚪ Common"

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
    if value >= 5000: return "🌌"
    if value >= 1000: return "🌠"
    if value >= 500:  return "👑"
    if value >= 100:  return "💎"
    if value >= 50:   return "🔥"
    if value >= 20:   return "⭐"
    if value >= 10:   return "🟡"
    if value >= 5:    return "🔵"
    if value >= 2:    return "⚪"
    return "🟤"

def coin_value_to_credits(value: float) -> int:
    return max(1, int(value))

def prestige_multiplier(prestige: int) -> float:
    return 1.0 + (prestige * 0.1)

# ─── DB Init ──────────────────────────────────────────────────────────────────
def init_db():
    conn = db()
    cur  = conn.cursor()

    # Check / rebuild coins table if id column missing
    cur.execute("SELECT EXISTS(SELECT FROM information_schema.tables WHERE table_name='coins')")
    coins_exists = cur.fetchone()['exists']
    if coins_exists:
        cur.execute("SELECT EXISTS(SELECT FROM information_schema.columns WHERE table_name='coins' AND column_name='id')")
        id_exists = cur.fetchone()['exists']
        if not id_exists:
            print("⚠️ coins table missing 'id' column! Rebuilding...")
            try:
                cur.execute("CREATE TABLE IF NOT EXISTS coins_backup AS SELECT * FROM coins")
                cur.execute("DROP TABLE coins CASCADE")
                conn.commit()
            except Exception as e:
                print(f"Table drop error: {e}")
                conn.rollback()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id          BIGINT PRIMARY KEY,
            username         TEXT,
            credits          INT DEFAULT 0,
            last_msg_ts      BIGINT DEFAULT 0,
            last_daily       DATE,
            daily_streak     INT DEFAULT 0,
            last_work_ts     BIGINT DEFAULT 0,
            last_rob_ts      BIGINT DEFAULT 0,
            prestige         INT DEFAULT 0,
            total_coins      INT DEFAULT 0,
            work_count       INT DEFAULT 0,
            inventory_public BOOLEAN DEFAULT FALSE,
            joined_at        TIMESTAMP DEFAULT NOW()
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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_bank (
            user_id       BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            balance       BIGINT DEFAULT 0,
            last_interest TIMESTAMP DEFAULT NOW(),
            bank_id       TEXT DEFAULT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS coin_trades (
            id          SERIAL PRIMARY KEY,
            coin_id     INT,
            seller_id   BIGINT,
            buyer_id    BIGINT,
            price       INT NOT NULL,
            traded_at   TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_event_log (
            id         SERIAL PRIMARY KEY,
            event_type TEXT,
            material   TEXT,
            multiplier FLOAT,
            started_at TIMESTAMP DEFAULT NOW(),
            expires_at TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS material_sales_log (
            material   TEXT PRIMARY KEY,
            total_sold INT DEFAULT 0,
            last_reset TIMESTAMP DEFAULT NOW()
        )
    """)
    for mat in MATERIAL_PRICE_RANGES:
        cur.execute("""
            INSERT INTO material_sales_log (material, total_sold)
            VALUES (%s, 0) ON CONFLICT (material) DO NOTHING
        """, (mat,))

    # Restore from backup if needed
    try:
        cur.execute("SELECT COUNT(*) as c FROM coins_backup")
        backup_count = cur.fetchone()['c']
        if backup_count > 0:
            print(f"🔄 Restoring {backup_count} coins from backup...")
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='coins' ORDER BY ordinal_position")
            new_cols = [row['column_name'] for row in cur.fetchall()]
            col_list = ', '.join(new_cols)
            cur.execute(f"INSERT INTO coins ({col_list}) SELECT {col_list} FROM coins_backup WHERE owner_id IN (SELECT user_id FROM users)")
            conn.commit()
            cur.execute("DROP TABLE IF EXISTS coins_backup")
            conn.commit()
            print("✅ Backup data restored!")
    except Exception as e:
        print(f"Backup restore notice: {e}")
        conn.rollback()

    # Migrate users columns
    existing_user_cols = set()
    try:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='users'")
        existing_user_cols = {row['column_name'] for row in cur.fetchall()}
    except Exception:
        pass

    user_columns = [
        ("last_msg_ts",      "BIGINT DEFAULT 0"),
        ("last_work_ts",     "BIGINT DEFAULT 0"),
        ("last_rob_ts",      "BIGINT DEFAULT 0"),
        ("prestige",         "INT DEFAULT 0"),
        ("total_coins",      "INT DEFAULT 0"),
        ("daily_streak",     "INT DEFAULT 0"),
        ("joined_at",        "TIMESTAMP DEFAULT NOW()"),
        ("credits",          "INT DEFAULT 0"),
        ("work_count",       "INT DEFAULT 0"),
        ("inventory_public", "BOOLEAN DEFAULT FALSE"),
    ]
    for col, col_def in user_columns:
        if col not in existing_user_cols:
            try:
                cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {col_def}")
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"User migration {col}: {e}")

    # Migrate user_bank: add bank_id if missing
    try:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='user_bank'")
        bank_cols = {row['column_name'] for row in cur.fetchall()}
        if 'bank_id' not in bank_cols:
            cur.execute("ALTER TABLE user_bank ADD COLUMN IF NOT EXISTS bank_id TEXT DEFAULT NULL")
            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"user_bank migration: {e}")

    # Trades table
    cur.execute("SELECT EXISTS(SELECT FROM information_schema.tables WHERE table_name='trades')")
    trades_exists = cur.fetchone()['exists']
    if trades_exists:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='trades'")
        trade_cols = {row['column_name'] for row in cur.fetchall()}
        if 'from_user' in trade_cols and 'initiator_id' not in trade_cols:
            try:
                cur.execute("ALTER TABLE trades RENAME COLUMN from_user TO initiator_id")
                conn.commit()
            except Exception as e:
                conn.rollback()
        if 'to_user' in trade_cols and 'receiver_id' not in trade_cols:
            try:
                cur.execute("ALTER TABLE trades RENAME COLUMN to_user TO receiver_id")
                conn.commit()
            except Exception as e:
                conn.rollback()

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
    try:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='trades'")
        trade_cols_now = {row['column_name'] for row in cur.fetchall()}
        if 'initiator_id' not in trade_cols_now:
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS initiator_id BIGINT")
            conn.commit()
        if 'receiver_id' not in trade_cols_now:
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS receiver_id BIGINT")
            conn.commit()
    except Exception as e:
        conn.rollback()

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
    cur.execute("INSERT INTO bank(id,total) VALUES(1,0) ON CONFLICT(id) DO NOTHING")
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

    # ── User inventory / permanent items ──────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_items (
            user_id          BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            has_workshop      BOOLEAN DEFAULT FALSE,
            has_cognitive_machine BOOLEAN DEFAULT FALSE,
            cognitive_enabled BOOLEAN DEFAULT FALSE,
            last_cognitive_ts BIGINT DEFAULT 0,
            has_ai_machine    BOOLEAN DEFAULT FALSE,
            ai_enabled        BOOLEAN DEFAULT FALSE,
            last_ai_ts        BIGINT DEFAULT 0,
            has_dlg           BOOLEAN DEFAULT FALSE,
            has_sos           BOOLEAN DEFAULT FALSE,
            has_transfer      BOOLEAN DEFAULT FALSE
        )
    """)

    # ── Hacking system ────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS hack_progress (
            id           SERIAL PRIMARY KEY,
            hacker_id    BIGINT,
            target_bank_id TEXT,
            fail_count   INT DEFAULT 0,
            updated_at   TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS hack_transfers (
            id           SERIAL PRIMARY KEY,
            hacker_id    BIGINT,
            amount       INT,
            completes_at TIMESTAMP,
            done         BOOLEAN DEFAULT FALSE,
            created_at   TIMESTAMP DEFAULT NOW()
        )
    """)

    # ── Black market purchases log ────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS black_market_log (
            id          SERIAL PRIMARY KEY,
            user_id     BIGINT,
            item_key    TEXT,
            price       INT,
            bought_at   TIMESTAMP DEFAULT NOW()
        )
    """)

    # ── Data cards (for hacking) ──────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS data_cards (
            id           SERIAL PRIMARY KEY,
            owner_id     BIGINT,
            target_bank_id TEXT,
            fail_count_snapshot INT,
            created_at   TIMESTAMP DEFAULT NOW()
        )
    """)

    conn.commit()
    print("✅ Database initialized!")
    release(conn)

# ─── Helpers ──────────────────────────────────────────────────────────────────
def ensure_user(user_id: int, username: str):
    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO users (user_id, username, credits, last_msg_ts, daily_streak,
                               last_work_ts, last_rob_ts, prestige, total_coins, work_count, inventory_public)
            VALUES (%s, %s, 0, 0, 0, 0, 0, 0, 0, 0, FALSE)
            ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
        """, (user_id, username))
        cur.execute("""
            INSERT INTO user_bank (user_id, balance, last_interest)
            VALUES (%s, 0, NOW())
            ON CONFLICT (user_id) DO NOTHING
        """, (user_id,))
        # Generate bank_id if missing
        cur.execute("SELECT bank_id FROM user_bank WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row and not row['bank_id']:
            bank_id = generate_bank_id()
            cur.execute("UPDATE user_bank SET bank_id = %s WHERE user_id = %s", (bank_id, user_id))
        cur.execute("""
            INSERT INTO user_items (user_id) VALUES (%s)
            ON CONFLICT (user_id) DO NOTHING
        """, (user_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"ensure_user error: {e}")
    finally:
        release(conn)

def generate_bank_id() -> str:
    """Generate a complex bank account ID."""
    import string
    chars   = string.ascii_uppercase + string.digits
    segment = lambda n: ''.join(random.choices(chars, k=n))
    return f"CVB-{segment(4)}-{segment(6)}-{segment(4)}-{segment(8)}"

def get_user(user_id: int):
    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row:
            defaults = {
                'credits': 0, 'last_msg_ts': 0, 'last_work_ts': 0, 'last_rob_ts': 0,
                'prestige': 0, 'total_coins': 0, 'daily_streak': 0, 'work_count': 0,
                'inventory_public': False,
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

def get_user_items(user_id: int) -> dict:
    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT * FROM user_items WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO user_items (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
            conn.commit()
            cur.execute("SELECT * FROM user_items WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
        return dict(row) if row else {}
    except Exception as e:
        print(f"get_user_items error: {e}")
        return {}
    finally:
        release(conn)

def sync_coin_count(uid: int, cur):
    cur.execute(
        "UPDATE users SET total_coins = (SELECT COUNT(*) FROM coins WHERE owner_id = %s) WHERE user_id = %s",
        (uid, uid)
    )

def add_credits(user_id: int, amount: int, reason: str = ""):
    conn = db()
    cur  = conn.cursor()
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
    cur  = conn.cursor()
    try:
        cur.execute("SELECT total FROM bank WHERE id = 1")
        row = cur.fetchone()
        return row['total'] if row else 0
    finally:
        release(conn)

def count_users():
    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) as c FROM users")
        row = cur.fetchone()
        return row['c'] if row else 1
    finally:
        release(conn)

def get_portfolio_value(uid: int) -> float:
    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT COALESCE(SUM(value), 0) as pv FROM coins WHERE owner_id = %s", (uid,))
        row = cur.fetchone()
        return float(row['pv']) if row else 0.0
    finally:
        release(conn)

def get_user_bank(user_id: int) -> dict:
    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT * FROM user_bank WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if not row:
            bank_id = generate_bank_id()
            cur.execute("""
                INSERT INTO user_bank (user_id, balance, last_interest, bank_id)
                VALUES (%s, 0, NOW(), %s) ON CONFLICT DO NOTHING
            """, (user_id, bank_id))
            conn.commit()
            return {"balance": 0, "last_interest": datetime.now(timezone.utc), "bank_id": bank_id}
        return dict(row)
    except Exception as e:
        print(f"get_user_bank error: {e}")
        return {"balance": 0, "last_interest": datetime.now(timezone.utc), "bank_id": None}
    finally:
        release(conn)

def apply_bank_interest(user_id: int) -> int:
    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT * FROM user_bank WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if not row or row['balance'] <= 0:
            return 0
        now  = datetime.now(timezone.utc)
        last = row['last_interest']
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed_mins = (now - last).total_seconds() / 60
        periods      = int(elapsed_mins // BANK_INTEREST_MINS)
        if periods <= 0:
            return 0
        old_balance = row['balance']
        new_balance = int(old_balance * (1 + BANK_INTEREST_PCT) ** periods)
        interest    = new_balance - old_balance
        cur.execute("UPDATE user_bank SET balance = %s, last_interest = %s WHERE user_id = %s",
                    (new_balance, now, user_id))
        conn.commit()
        return interest
    except Exception as e:
        conn.rollback()
        print(f"apply_bank_interest error: {e}")
        return 0
    finally:
        release(conn)

def get_coin_rap(coin_id: int):
    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT AVG(price) as rap FROM coin_trades
            WHERE coin_id = %s ORDER BY traded_at DESC LIMIT 10
        """, (coin_id,))
        row = cur.fetchone()
        if row and row['rap'] is not None:
            return round(float(row['rap']), 2)
        return None
    except Exception as e:
        print(f"get_coin_rap error: {e}")
        return None
    finally:
        release(conn)

def get_coin_trade_history(coin_id: int) -> list:
    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT ct.*, us.username as seller_name, ub.username as buyer_name
            FROM coin_trades ct
            LEFT JOIN users us ON us.user_id = ct.seller_id
            LEFT JOIN users ub ON ub.user_id = ct.buyer_id
            WHERE ct.coin_id = %s ORDER BY ct.traded_at DESC LIMIT 5
        """, (coin_id,))
        return cur.fetchall()
    except Exception as e:
        print(f"get_coin_trade_history error: {e}")
        return []
    finally:
        release(conn)

def log_material_sale_db(material: str):
    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO material_sales_log (material, total_sold)
            VALUES (%s, 1)
            ON CONFLICT (material) DO UPDATE
            SET total_sold = material_sales_log.total_sold + 1
        """, (material,))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        release(conn)

# ─── Black Market Helpers ──────────────────────────────────────────────────────
BLACK_MARKET_ITEMS = {
    "identity_tracker": {
        "name":  "Identity Tracker",
        "price": 5100,
        "stock": 10,
        "chance": 0.60,
        "desc":  "One-time use: reveals a user's bank account ID. Result sent to your DMs.",
        "emoji": "🕵️",
    },
    "data_leak_generator": {
        "name":  "Data Leak Generator",
        "price": 15500,
        "stock": 1,
        "chance": 0.30,
        "desc":  "Permanent. Each failed bank hack gives you a Data Card (+1% hack success, max 51%).",
        "emoji": "💾",
    },
    "suspicious_os": {
        "name":  "Suspicious Operating System",
        "price": 41000,
        "stock": 1,
        "chance": 0.70,
        "desc":  "Permanent. Unlocks hacking commands in DMs.",
        "emoji": "💻",
    },
    "transfer_system": {
        "name":  "Transfer System",
        "price": 27000,
        "stock": 1,
        "chance": 0.65,
        "desc":  "Permanent. Transfers hacked credits in 30 minutes.",
        "emoji": "📡",
    },
    "ai_machine": {
        "name":  "Artificial Intelligence Machine",
        "price": 50000,
        "stock": 1,
        "chance": 0.20,
        "desc":  "Permanent. Auto-works every 15 min at 2× salary. Toggle with -work.",
        "emoji": "🤖",
    },
}

def reset_black_market_stock():
    global _black_market_stock
    _black_market_stock = {}
    for key, item in BLACK_MARKET_ITEMS.items():
        if random.random() < item["chance"]:
            _black_market_stock[key] = item["stock"]

def get_hack_progress(hacker_id: int, target_bank_id: str) -> int:
    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute(
            "SELECT fail_count FROM hack_progress WHERE hacker_id=%s AND target_bank_id=%s",
            (hacker_id, target_bank_id)
        )
        row = cur.fetchone()
        return row['fail_count'] if row else 0
    finally:
        release(conn)

def increment_hack_fail(hacker_id: int, target_bank_id: str):
    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO hack_progress (hacker_id, target_bank_id, fail_count)
            VALUES (%s, %s, 1)
            ON CONFLICT (hacker_id, target_bank_id)
            DO UPDATE SET fail_count = hack_progress.fail_count + 1, updated_at = NOW()
        """, (hacker_id, target_bank_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"increment_hack_fail error: {e}")
    finally:
        release(conn)

# ─── Spam Prevention ──────────────────────────────────────────────────────────
MSG_COOLDOWNS = {}

# ─── Bot Setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='-', intents=intents, help_command=None)

_announce_channel_id = None

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
        u           = get_user(uid)
        prestige_val = u['prestige'] if u and u.get('prestige') else 0
        bonus        = int(CREDITS_PER_MSG * prestige_multiplier(prestige_val))
        add_credits(uid, bonus, "message")
    await bot.process_commands(message)

# ─── Background Tasks ─────────────────────────────────────────────────────────
@tasks.loop(minutes=20)
async def update_market_prices():
    event = generate_market_prices()
    if event and _announce_channel_id:
        ch = bot.get_channel(_announce_channel_id)
        if ch:
            ev_type, mat, mult = event
            if ev_type == "boom":
                msg = (f"📈 **MARKET BOOM!** The **{mat}** market is surging! "
                       f"Prices are **×{mult}** for the next {EVENT_DURATION_MINS} minutes! 💰")
            else:
                msg = (f"📉 **MARKET BUST!** The **{mat}** market is crashing! "
                       f"Prices dropped to **×{mult}** for {EVENT_DURATION_MINS} minutes. 🔻")
            try:
                await ch.send(msg)
            except Exception:
                pass

@tasks.loop(minutes=5)
async def auction_checker():
    conn = db()
    cur  = conn.cursor()
    try:
        now = datetime.now(timezone.utc)
        cur.execute("SELECT * FROM auctions WHERE status='active' AND ends_at <= %s", (now,))
        expired = cur.fetchall()
        for a in expired:
            coin_id   = a['coin_id']
            seller_id = a['seller_id']
            if a['bidder_id'] and a['current_bid']:
                winner_id  = a['bidder_id']
                sale_price = a['current_bid']
                fee        = int(round(sale_price * MARKET_FEE_PCT))
                sink       = int(round(sale_price * ECONOMY_SINK_SELL_PCT))
                seller_net = sale_price - fee - sink
                cur.execute("UPDATE coins SET owner_id=%s WHERE id=%s", (winner_id, coin_id))
                cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s", (seller_net, seller_id))
                cur.execute("UPDATE bank SET total=total+%s WHERE id=1", (fee,))
                cur.execute("INSERT INTO bank_log(source,amount) VALUES(%s,%s)", (f"auction_fee:{a['id']}", fee))
                cur.execute("INSERT INTO bank_log(source,amount) VALUES(%s,%s)", (f"auction_sink:{a['id']}", -sink))
                cur.execute("INSERT INTO coin_trades(coin_id,seller_id,buyer_id,price) VALUES(%s,%s,%s,%s)",
                            (coin_id, seller_id, winner_id, sale_price))
                sync_coin_count(winner_id, cur)
                sync_coin_count(seller_id, cur)
                cur2conn = db()
                cur2 = cur2conn.cursor()
                cur2.execute("SELECT material FROM coins WHERE id=%s", (coin_id,))
                mat_row = cur2.fetchone()
                release(cur2conn)
                if mat_row:
                    record_material_sale(mat_row['material'])
                    log_material_sale_db(mat_row['material'])
                cur.execute("UPDATE auctions SET status='sold' WHERE id=%s", (a['id'],))
                conn.commit()
                try:
                    winner = await bot.fetch_user(winner_id)
                    c_conn = db(); c_cur = c_conn.cursor()
                    c_cur.execute("SELECT * FROM coins WHERE id=%s", (coin_id,))
                    c = c_cur.fetchone(); release(c_conn)
                    if winner and c:
                        await winner.send(f"🎉 You won auction **#{a['id']}**! **{coin_name(c)}** is now yours for **{sale_price:,} credits**.")
                except Exception: pass
                try:
                    seller = await bot.fetch_user(seller_id)
                    if seller:
                        await seller.send(f"✅ Auction **#{a['id']}** sold for **{sale_price:,} credits**. You received **{seller_net:,}** after fees.")
                except Exception: pass
            else:
                cur.execute("UPDATE coins SET owner_id=%s WHERE id=%s", (seller_id, coin_id))
                cur.execute("UPDATE auctions SET status='expired' WHERE id=%s", (a['id'],))
                conn.commit()
                try:
                    seller = await bot.fetch_user(seller_id)
                    if seller:
                        await seller.send(f"📦 Auction **#{a['id']}** expired with no bids. Coin returned.")
                except Exception: pass
    except Exception as e:
        print(f"auction_checker error: {e}")
        try: conn.rollback()
        except Exception: pass
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
    cur  = conn.cursor()
    try:
        today = datetime.now(timezone.utc).date()
        cur.execute("SELECT paid_date FROM daily_log WHERE paid_date=%s", (today,))
        if cur.fetchone():
            return
        cur.execute("UPDATE users SET credits=credits+%s", (share,))
        cur.execute("UPDATE bank SET total=0 WHERE id=1")
        cur.execute("INSERT INTO daily_log(paid_date,amount) VALUES(%s,%s)", (today, share))
        conn.commit()
        print(f"✅ Daily bank payout: {share:,} credits to {n} users.")
    except Exception as e:
        conn.rollback()
        print(f"daily_bank_distribution error: {e}")
    finally:
        release(conn)

@tasks.loop(hours=2)
async def black_market_spawner():
    global _black_market_active, _black_market_expires
    now = datetime.now(timezone.utc)
    # Clear expired
    if _black_market_active and _black_market_expires and now >= _black_market_expires:
        _black_market_active  = False
        _black_market_expires = None
        _black_market_stock   = {}
        ch = bot.get_channel(BLACK_MARKET_CHANNEL_ID)
        if ch:
            try:
                await ch.send("🌑 The **Black Market** has vanished into the shadows...")
            except Exception: pass
        return

    if not _black_market_active and random.random() < BLACK_MARKET_CHANCE:
        reset_black_market_stock()
        _black_market_active  = True
        _black_market_expires = now + timedelta(hours=BLACK_MARKET_DURATION_H)
        ch = bot.get_channel(BLACK_MARKET_CHANNEL_ID)
        if ch:
            try:
                embed = discord.Embed(
                    title="🌑 BLACK MARKET APPEARED!",
                    description=(
                        f"A shadowy marketplace has emerged for the next **{BLACK_MARKET_DURATION_H} hours**!\n"
                        f"Use `-blackmarket` to browse exclusive items.\n"
                        f"⚠️ These items are rare — stock is extremely limited!"
                    ),
                    color=0x2F3136
                )
                embed.set_footer(text=f"Expires: {_black_market_expires.strftime('%H:%M UTC')}")
                await ch.send("@everyone", embed=embed)
            except Exception as e:
                print(f"black market announce error: {e}")

@tasks.loop(minutes=5)
async def auto_worker_task():
    """Handle Cognitive Machine and AI Machine auto-work."""
    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT * FROM user_items WHERE (cognitive_enabled=TRUE OR ai_enabled=TRUE)")
        rows = cur.fetchall()
    except Exception as e:
        print(f"auto_worker_task fetch error: {e}")
        release(conn)
        return
    finally:
        release(conn)

    now_ts = int(time.time())
    for item_row in rows:
        uid = item_row['user_id']
        u   = get_user(uid)
        if not u:
            continue

        work_count   = u.get('work_count') or 0
        prestige_val = u.get('prestige') or 0
        rank_name, rank_salary, rank_emoji, rank_idx = get_job_rank(work_count)
        prev_rank_min = JOB_RANKS[rank_idx][1]
        jobs_in_rank  = work_count - prev_rank_min
        within_rank_mult = min(1.0 + (jobs_in_rank * 0.02), 1.5)
        base_pay = int(rank_salary * within_rank_mult)
        base_pay = min(base_pay, 5000)

        # Cognitive Machine: every 30 min, 0.75× salary
        if item_row['cognitive_enabled']:
            cd = 1800  # 30 min
            elapsed = now_ts - (item_row['last_cognitive_ts'] or 0)
            if elapsed >= cd:
                earned = int(base_pay * prestige_multiplier(prestige_val) * 0.75)
                conn2 = db(); cur2 = conn2.cursor()
                try:
                    cur2.execute("UPDATE users SET credits=credits+%s, work_count=work_count+1 WHERE user_id=%s", (earned, uid))
                    cur2.execute("UPDATE user_items SET last_cognitive_ts=%s WHERE user_id=%s", (now_ts, uid))
                    cur2.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,'cognitive_auto_work')", (uid, earned))
                    conn2.commit()
                except Exception as e:
                    conn2.rollback()
                    print(f"cognitive auto-work error uid={uid}: {e}")
                finally:
                    release(conn2)

        # AI Machine: every 15 min, 2× salary
        if item_row['ai_enabled']:
            cd = 900  # 15 min
            elapsed = now_ts - (item_row['last_ai_ts'] or 0)
            if elapsed >= cd:
                earned = int(base_pay * prestige_multiplier(prestige_val) * 2.0)
                conn2 = db(); cur2 = conn2.cursor()
                try:
                    cur2.execute("UPDATE users SET credits=credits+%s, work_count=work_count+1 WHERE user_id=%s", (earned, uid))
                    cur2.execute("UPDATE user_items SET last_ai_ts=%s WHERE user_id=%s", (now_ts, uid))
                    cur2.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,'ai_auto_work')", (uid, earned))
                    conn2.commit()
                except Exception as e:
                    conn2.rollback()
                    print(f"AI machine auto-work error uid={uid}: {e}")
                finally:
                    release(conn2)

@tasks.loop(minutes=1)
async def hack_transfer_checker():
    """Complete pending hack transfers."""
    conn = db()
    cur  = conn.cursor()
    try:
        now = datetime.now(timezone.utc)
        cur.execute("SELECT * FROM hack_transfers WHERE done=FALSE AND completes_at <= %s", (now,))
        rows = cur.fetchall()
        for r in rows:
            cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s", (r['amount'], r['hacker_id']))
            cur.execute("UPDATE hack_transfers SET done=TRUE WHERE id=%s", (r['id'],))
            cur.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,'hack_transfer')",
                        (r['hacker_id'], r['amount']))
            conn.commit()
            try:
                user = await bot.fetch_user(r['hacker_id'])
                if user:
                    await user.send(f"📡 **Transfer Complete!** **{r['amount']:,} credits** from your hack have been deposited to your wallet.")
            except Exception: pass
    except Exception as e:
        print(f"hack_transfer_checker error: {e}")
        try: conn.rollback()
        except Exception: pass
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
        cur  = conn.cursor()
        try:
            cur.execute("SELECT * FROM trades WHERE id=%s AND status='pending'", (self.trade_id,))
            trade = cur.fetchone()
            if not trade:
                await interaction.response.send_message("⚠️ Trade no longer active.", ephemeral=True)
                return

            if not accepted:
                cur.execute("UPDATE trades SET status='declined' WHERE id=%s", (self.trade_id,))
                conn.commit()
                await interaction.response.edit_message(content="❌ Trade declined.", embed=None, view=None)
                self.stop()
                return

            coin_ids      = [int(x) for x in trade['coin_ids'].split(',') if x.strip()] if trade['coin_ids'] else []
            credits_offer = trade['credits_offer']

            if coin_ids:
                placeholders = ','.join(['%s'] * len(coin_ids))
                cur.execute(f"SELECT id, owner_id FROM coins WHERE id IN ({placeholders})", coin_ids)
                rows = cur.fetchall()
                for r in rows:
                    if r['owner_id'] != trade['initiator_id']:
                        cur.execute("UPDATE trades SET status='invalid' WHERE id=%s", (self.trade_id,))
                        conn.commit()
                        await interaction.response.edit_message(content="❌ Coin ownership changed; trade cancelled.", embed=None, view=None)
                        self.stop()
                        return

            if credits_offer > 0:
                cur.execute("SELECT credits FROM users WHERE user_id=%s", (trade['initiator_id'],))
                init_user = cur.fetchone()
                if not init_user or init_user['credits'] < credits_offer:
                    cur.execute("UPDATE trades SET status='invalid' WHERE id=%s", (self.trade_id,))
                    conn.commit()
                    await interaction.response.edit_message(content="❌ Initiator has insufficient credits.", embed=None, view=None)
                    self.stop()
                    return

            if coin_ids:
                placeholders = ','.join(['%s'] * len(coin_ids))
                cur.execute(f"UPDATE coins SET owner_id=%s WHERE id IN ({placeholders})",
                            [trade['receiver_id']] + coin_ids)
                per_coin_price = credits_offer // len(coin_ids) if coin_ids else 0
                for cid in coin_ids:
                    cur.execute("INSERT INTO coin_trades(coin_id,seller_id,buyer_id,price) VALUES(%s,%s,%s,%s)",
                                (cid, trade['initiator_id'], trade['receiver_id'], per_coin_price))

            if credits_offer > 0:
                tax  = int(round(credits_offer * TRADE_TAX_PCT))
                sink = int(round(credits_offer * ECONOMY_SINK_SELL_PCT))
                net  = credits_offer - tax - sink
                cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s", (credits_offer, trade['initiator_id']))
                cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s", (net, trade['receiver_id']))
                cur.execute("UPDATE bank SET total=total+%s WHERE id=1", (tax,))
                cur.execute("INSERT INTO bank_log(source,amount) VALUES(%s,%s)", (f"trade_tax:{self.trade_id}", tax))
                cur.execute("INSERT INTO bank_log(source,amount) VALUES(%s,%s)", (f"trade_sink:{self.trade_id}", -sink))

            for uid in (trade['initiator_id'], trade['receiver_id']):
                sync_coin_count(uid, cur)

            cur.execute("UPDATE trades SET status='completed' WHERE id=%s", (self.trade_id,))
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
            except Exception: pass
        finally:
            release(conn)

# ─── Auction Views ────────────────────────────────────────────────────────────
class BidModal(discord.ui.Modal, title="Place a Bid"):
    amount = discord.ui.TextInput(label="Bid Amount (credits)", placeholder="e.g. 500")

    def __init__(self, auction_id: int):
        super().__init__()
        self.auction_id = auction_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            bid = int(self.amount.value)
        except ValueError:
            await interaction.response.send_message("❌ Enter a whole number.", ephemeral=True)
            return

        uid = interaction.user.id
        ensure_user(uid, str(interaction.user))
        conn = db(); cur = conn.cursor()
        try:
            cur.execute("SELECT * FROM auctions WHERE id=%s AND status='active'", (self.auction_id,))
            a = cur.fetchone()
            if not a:
                await interaction.response.send_message("❌ Auction not found or ended.", ephemeral=True)
                return
            if uid == a['seller_id']:
                await interaction.response.send_message("❌ Can't bid on your own auction.", ephemeral=True)
                return
            min_bid = max(a['start_price'], (a['current_bid'] or 0) + 1)
            if bid < min_bid:
                await interaction.response.send_message(f"❌ Minimum bid: **{min_bid:,} credits**.", ephemeral=True)
                return
            cur.execute("SELECT credits FROM users WHERE user_id=%s", (uid,))
            user = cur.fetchone()
            if not user or user['credits'] < bid:
                bal = user['credits'] if user else 0
                await interaction.response.send_message(f"❌ Insufficient credits. You have **{bal:,}**.", ephemeral=True)
                return
            if a['bidder_id'] and a['current_bid']:
                cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s", (a['current_bid'], a['bidder_id']))
            cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s", (bid, uid))
            cur.execute("UPDATE auctions SET current_bid=%s, bidder_id=%s WHERE id=%s", (bid, uid, self.auction_id))
            conn.commit()
            await interaction.response.send_message(f"✅ Bid of **{bid:,} credits** placed on auction **#{self.auction_id}**!", ephemeral=True)
        except Exception as e:
            conn.rollback()
            print(f"BidModal error: {e}")
            try:
                await interaction.response.send_message("❌ An error occurred placing your bid.", ephemeral=True)
            except Exception: pass
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
        won    = (choice == result)
        conn = db(); cur = conn.cursor()
        try:
            if won:
                cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s", (self.bet, self.uid))
                bank_cut = max(1, int(self.bet * 0.02))
                cur.execute("UPDATE bank SET total=total+%s WHERE id=1", (bank_cut,))
                cur.execute("INSERT INTO bank_log(source,amount) VALUES('gamble_tax',%s)", (bank_cut,))
                color, title = discord.Color.green(), "🎉 You Won!"
                desc = f"The coin landed **{result}**! You win **{self.bet:,} credits**."
            else:
                cur.execute("UPDATE users SET credits=GREATEST(0,credits-%s) WHERE user_id=%s", (self.bet, self.uid))
                bank_cut = max(1, int(self.bet * 0.50))
                cur.execute("UPDATE bank SET total=total+%s WHERE id=1", (bank_cut,))
                cur.execute("INSERT INTO bank_log(source,amount) VALUES('gamble_house',%s)", (bank_cut,))
                color, title = discord.Color.red(), "💸 You Lost!"
                desc = f"The coin landed **{result}**. You lose **{self.bet:,} credits**."
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

# ─── Hacking Minigame Views ───────────────────────────────────────────────────
class HackChallengeView(discord.ui.View):
    """Multi-stage hacking minigame sent in DMs."""

    def __init__(self, hacker_id: int, target_bank_id: str, target_uid: int, hack_chance: float):
        super().__init__(timeout=120)
        self.hacker_id      = hacker_id
        self.target_bank_id = target_bank_id
        self.target_uid     = target_uid
        self.hack_chance    = hack_chance
        self.stage          = 0
        self.passed_stages  = 0
        self.required_stages = 3
        self._gen_stage()

    def _gen_stage(self):
        """Generate a random challenge for the current stage."""
        stage_types = ["math", "sequence", "password"]
        self.current_type = random.choice(stage_types)

        if self.current_type == "math":
            a, b = random.randint(10, 99), random.randint(10, 99)
            op   = random.choice(['+', '-', '*'])
            self.challenge_text = f"Solve: `{a} {op} {b}`"
            self.correct_answer = str(eval(f"{a}{op}{b}"))

        elif self.current_type == "sequence":
            seq = [random.randint(1, 9) for _ in range(5)]
            hidden_idx   = random.randint(1, 3)
            display      = [str(n) if i != hidden_idx else "?" for i, n in enumerate(seq)]
            self.challenge_text = f"Find the missing number: `{' '.join(display)}`"
            self.correct_answer = str(seq[hidden_idx])

        elif self.current_type == "password":
            chars    = "ABCDE12345"
            password = ''.join(random.choices(chars, k=4))
            scrambled = list(password)
            random.shuffle(scrambled)
            self.challenge_text = f"Unscramble the 4-character code: `{''.join(scrambled)}`\n*(Type the correct order)*"
            self.correct_answer = password

class HackInputModal(discord.ui.Modal, title="🔓 Hacking Terminal"):
    answer = discord.ui.TextInput(label="Your Answer", placeholder="Enter your answer...")

    def __init__(self, hack_view: 'HackChallengeView', dm_message):
        super().__init__()
        self.hack_view  = hack_view
        self.dm_message = dm_message

    async def on_submit(self, interaction: discord.Interaction):
        user_ans = self.answer.value.strip()
        correct  = (user_ans == self.hack_view.correct_answer)

        if correct:
            self.hack_view.passed_stages += 1

        self.hack_view.stage += 1

        if self.hack_view.stage >= self.hack_view.required_stages:
            await interaction.response.defer()
            await self._finish_hack(interaction)
        else:
            self.hack_view._gen_stage()
            status = "✅ Correct!" if correct else f"❌ Wrong! Answer was `{self.hack_view.correct_answer}`"
            embed  = discord.Embed(
                title=f"💻 Hacking in Progress — Stage {self.hack_view.stage + 1}/{self.hack_view.required_stages}",
                description=(
                    f"{status}\n\n"
                    f"**Stage {self.hack_view.stage + 1}:** {self.hack_view.challenge_text}\n\n"
                    f"Progress: {'✅' * self.hack_view.passed_stages}{'⬜' * (self.hack_view.required_stages - self.hack_view.passed_stages)}"
                ),
                color=0x00FF41
            )
            view = HackStageView(self.hack_view, self.dm_message)
            await interaction.response.defer()
            try:
                await self.dm_message.edit(embed=embed, view=view)
            except Exception: pass

    async def _finish_hack(self, interaction):
        hv = self.hack_view
        # Final roll: chance boosted by stages passed
        stage_bonus = hv.passed_stages / hv.required_stages * 0.20
        final_chance = min(hv.hack_chance + stage_bonus, HACK_MAX_CHANCE)

        roll    = random.random()
        success = roll < final_chance

        target_bank = get_user_bank(hv.target_uid)
        items       = get_user_items(hv.hacker_id)

        if success:
            steal = random.randint(HACK_MIN_EARN, HACK_MAX_EARN)
            steal = min(steal, target_bank['balance'])
            steal = max(0, steal)

            conn = db(); cur = conn.cursor()
            try:
                cur.execute("UPDATE user_bank SET balance=GREATEST(0,balance-%s) WHERE user_id=%s",
                            (steal, hv.target_uid))
                if items.get('has_transfer'):
                    completes = datetime.now(timezone.utc) + timedelta(minutes=TRANSFER_DELAY_MINS)
                    cur.execute("INSERT INTO hack_transfers(hacker_id,amount,completes_at) VALUES(%s,%s,%s)",
                                (hv.hacker_id, steal, completes))
                    result_msg = (f"✅ **HACK SUCCESSFUL!**\nStole **{steal:,} credits** from the vault.\n"
                                  f"📡 Transfer in progress... funds arrive in **{TRANSFER_DELAY_MINS} minutes**.")
                else:
                    cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s", (steal, hv.hacker_id))
                    cur.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,'hack')",
                                (hv.hacker_id, steal))
                    result_msg = (f"✅ **HACK SUCCESSFUL!**\nStole **{steal:,} credits** from the vault.\n"
                                  f"⚠️ Install a **Transfer System** to avoid direct wallet exposure!")
                conn.commit()
            except Exception as e:
                conn.rollback()
                result_msg = f"❌ Hack succeeded but transfer error: {e}"
            finally:
                release(conn)
            color = 0x00FF41
        else:
            # Penalty: victim gets 10% of hacker's bank
            hacker_bank = get_user_bank(hv.hacker_id)
            penalty     = int(hacker_bank['balance'] * HACK_PENALTY_PCT)
            conn = db(); cur = conn.cursor()
            try:
                if penalty > 0:
                    cur.execute("UPDATE user_bank SET balance=GREATEST(0,balance-%s) WHERE user_id=%s",
                                (penalty, hv.hacker_id))
                    cur.execute("UPDATE user_bank SET balance=balance+%s WHERE user_id=%s",
                                (penalty, hv.target_uid))
                # Record fail + data card
                increment_hack_fail(hv.hacker_id, hv.target_bank_id)
                if items.get('has_dlg'):
                    fail_count = get_hack_progress(hv.hacker_id, hv.target_bank_id)
                    cur.execute("INSERT INTO data_cards(owner_id,target_bank_id,fail_count_snapshot) VALUES(%s,%s,%s)",
                                (hv.hacker_id, hv.target_bank_id, fail_count))
                conn.commit()
            except Exception as e:
                conn.rollback()
            finally:
                release(conn)
            penalty_str = f"\n💸 **{penalty:,} credits** deducted from your bank as counter-measure!" if penalty > 0 else ""
            dlg_str     = "\n💾 **Data Card** generated! Your success chance increased by 1%." if items.get('has_dlg') else ""
            result_msg  = f"❌ **HACK FAILED!**\nThe security system blocked you.{penalty_str}{dlg_str}"
            color = 0xFF0000

        embed = discord.Embed(title="💻 Hacking Terminal — Final Result", description=result_msg, color=color)
        embed.add_field(name="🎯 Success Chance", value=f"{final_chance*100:.1f}%", inline=True)
        embed.add_field(name="🎲 Roll", value=f"{roll*100:.1f}%", inline=True)
        embed.add_field(name="✅ Stages Passed", value=f"{hv.passed_stages}/{hv.required_stages}", inline=True)
        try:
            await self.dm_message.edit(embed=embed, view=None)
        except Exception: pass

class HackStageView(discord.ui.View):
    def __init__(self, hack_view: HackChallengeView, dm_message):
        super().__init__(timeout=120)
        self.hack_view  = hack_view
        self.dm_message = dm_message

    @discord.ui.button(label="💻 Enter Answer", style=discord.ButtonStyle.green)
    async def enter_answer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.hack_view.hacker_id:
            await interaction.response.send_message("❌ Not your terminal.", ephemeral=True)
            return
        await interaction.response.send_modal(HackInputModal(self.hack_view, self.dm_message))

# ─── Black Market Purchase View ────────────────────────────────────────────────
class BlackMarketBuyView(discord.ui.View):
    def __init__(self, item_key: str, price: int, buyer_id: int):
        super().__init__(timeout=60)
        self.item_key = item_key
        self.price    = price
        self.buyer_id = buyer_id

    @discord.ui.button(label="💰 Purchase", style=discord.ButtonStyle.green)
    async def purchase(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.buyer_id:
            await interaction.response.send_message("❌ Not your transaction.", ephemeral=True)
            return

        if not _black_market_active:
            await interaction.response.send_message("❌ The Black Market has closed.", ephemeral=True)
            self.stop()
            return

        remaining = _black_market_stock.get(self.item_key, 0)
        if remaining <= 0:
            await interaction.response.send_message("❌ Out of stock!", ephemeral=True)
            self.stop()
            return

        uid = interaction.user.id
        u   = get_user(uid)
        if not u or u['credits'] < self.price:
            bal = u['credits'] if u else 0
            await interaction.response.send_message(f"❌ Need **{self.price:,}** credits. You have **{bal:,}**.", ephemeral=True)
            return

        # Apply item
        item_info = BLACK_MARKET_ITEMS[self.item_key]
        conn = db(); cur = conn.cursor()
        try:
            cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s", (self.price, uid))
            cur.execute("INSERT INTO black_market_log(user_id,item_key,price) VALUES(%s,%s,%s)",
                        (uid, self.item_key, self.price))

            col_map = {
                "data_leak_generator": "has_dlg",
                "suspicious_os":       "has_sos",
                "transfer_system":     "has_transfer",
                "ai_machine":          "has_ai_machine",
            }
            if self.item_key in col_map:
                col = col_map[self.item_key]
                cur.execute(f"UPDATE user_items SET {col}=TRUE WHERE user_id=%s", (uid,))

            conn.commit()
            _black_market_stock[self.item_key] = remaining - 1
        except Exception as e:
            conn.rollback()
            print(f"black market purchase error: {e}")
            await interaction.response.send_message("❌ Purchase failed. Try again.", ephemeral=True)
            return
        finally:
            release(conn)

        # Handle Identity Tracker separately (one-time use, sends DM)
        if self.item_key == "identity_tracker":
            # It's already deducted, we just note the purchase — usage is via -usetracker
            await interaction.response.send_message(
                f"✅ Purchased **{item_info['name']}**! Use `-usetracker @user` to reveal their bank ID.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"✅ Purchased **{item_info['name']}**! Check `-items` for your inventory.",
                ephemeral=True
            )
        self.stop()

# ─── EVENTS ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    init_db()
    generate_market_prices()
    if not auction_checker.is_running():        auction_checker.start()
    if not daily_bank_distribution.is_running(): daily_bank_distribution.start()
    if not update_market_prices.is_running():   update_market_prices.start()
    if not black_market_spawner.is_running():   black_market_spawner.start()
    if not auto_worker_task.is_running():       auto_worker_task.start()
    if not hack_transfer_checker.is_running():  hack_transfer_checker.start()
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
        await ctx.send("❌ An error occurred running that command. Please try again.")
    else:
        print(f"Unhandled error: {error}")

# ─── COMMANDS ─────────────────────────────────────────────────────────────────

@bot.command()
async def help(ctx):
    e = discord.Embed(title="🪙 CoinVault — Command Reference", color=0x5865F2)
    e.add_field(name="💰 Economy (Credits)", value=(
        "`-balance` — Your credits & stats\n"
        "`-daily` — Claim daily credits (streak bonuses!)\n"
        "`-work` — Work for credits / toggle auto-machines\n"
        "`-rob @user` — Attempt to rob someone (6h CD, 50% chance)\n"
        "`-gamble <amount>` — Coinflip bet\n"
        "`-slots <amount>` — Spin the slot machine\n"
        "`-prestige` — Spend 5,000 credits (+10% all earnings)\n"
        "`-cd` / `-cooldown` — Check your cooldowns\n"
    ), inline=False)
    e.add_field(name="💼 Job & Career", value=(
        "`-work` — Work your job (30min CD)\n"
        "`-jobrank` — View your job rank & career progress\n"
        "`-jobladder` — See all job ranks\n"
    ), inline=False)
    e.add_field(name="🏦 Virtual Bank", value=(
        "`-vbank` — View your virtual bank (5% per 10 min)\n"
        "`-deposit <amount|all>` — Deposit to bank\n"
        "`-withdraw <amount|all>` — Withdraw from bank\n"
        "`-mybankid` — View your secret bank account ID\n"
    ), inline=False)
    e.add_field(name="🛒 Shop", value=(
        "`-shop` — Browse shop\n"
        "`-buy crate [all]` — Open crate(s)\n"
        "`-buy float_changer <coin_id>` — 320 cr → re-roll float\n"
        "`-buy market_trigger` — 250 cr → trigger a market event\n"
        "`-buy workshop` — 2,000 cr → Crafting Workshop\n"
        "`-buy cognitive_machine` — 6,000 cr → auto-work 0.75×\n"
        "`-buy polish <coin_id>` — 150 cr → upgrade coin status\n"
        "`-buy rename <coin_id> <name>` — 200 cr → rename coin\n"
    ), inline=False)
    e.add_field(name="🌑 Black Market", value=(
        "`-blackmarket` — View if Black Market is active (random 10%/2h)\n"
        "`-bmbuy <item>` — Buy from Black Market\n"
        "`-usetracker @user` — Use Identity Tracker to reveal bank ID\n"
    ), inline=False)
    e.add_field(name="🔓 Hacking (DM only)", value=(
        "Requires: Suspicious OS + Data Leak Generator + Transfer System\n"
        "`-hack <bank_id>` — Attempt to hack a bank account (DM)\n"
        "`-hackinventory` — View hack items & data cards\n"
        "`-hacktransfers` — Check pending credit transfers\n"
    ), inline=False)
    e.add_field(name="🎒 Inventory & Coins", value=(
        "`-inventory [page]` / `-inv` — Your coins\n"
        "`-coin <id>` — Detailed coin view\n"
        "`-sell <id|all>` — Sell at live market price\n"
        "`-sellall` — Sell all coins\n"
        "`-items` — View owned permanent items\n"
        "`-privacy on/off` — Toggle inventory visibility\n"
    ), inline=False)
    e.add_field(name="🤝 Trading & Marketplace", value=(
        "`-trade @user [coin_ids] [credits:<amount>]` — Offer trade\n"
        "`-trades` — Your pending trades\n"
        "`-market [page]` — Browse auctions\n"
        "`-auction <coin_id> <price> [hours]` — List coin\n"
        "`-bid <auction_id>` — Bid on auction\n"
    ), inline=False)
    e.add_field(name="📊 Stats & Market", value=(
        "`-profile [@user]` | `-leaderboard` | `-richlist` | `-bank`\n"
        "`-prices [material/float/status]` — Live prices\n"
        "`-economy` — Economy overview\n"
        "`-marketevents` — Active boom/bust events\n"
    ), inline=False)
    e.set_footer(text="Black Market spawns randomly • Hacking requires 3 special items from Black Market")
    await ctx.send(embed=e)

@bot.command(aliases=['bal', 'wallet'])
async def balance(ctx):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    u = get_user(uid)
    if not u:
        await ctx.send("❌ Could not load your profile.")
        return

    portfolio    = get_portfolio_value(uid)
    prestige_val = u.get('prestige') or 0
    pmult        = prestige_multiplier(prestige_val)
    vbank        = get_user_bank(uid)
    work_count   = u.get('work_count') or 0
    rank_name, rank_salary, rank_emoji, _ = get_job_rank(work_count)

    e = discord.Embed(title=f"💳 {ctx.author.display_name}'s Balance", color=0x57F287)
    e.add_field(name="🎟️ Credits",        value=f"**{u['credits']:,}**",               inline=True)
    e.add_field(name="🏦 Bank Balance",    value=f"**{vbank['balance']:,}** credits",   inline=True)
    e.add_field(name="🪙 Coins Owned",     value=f"**{u['total_coins']}**",             inline=True)
    e.add_field(name="📈 Portfolio Value", value=f"**${portfolio:.4f}**",               inline=True)
    e.add_field(name="⭐ Prestige",        value=f"**{prestige_val}** (×{pmult:.1f})",  inline=True)
    e.add_field(name=f"{rank_emoji} Job",  value=f"**{rank_name}**",                    inline=True)
    await ctx.send(embed=e)

@bot.command()
async def daily(ctx):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    u = get_user(uid)
    if not u:
        await ctx.send("❌ Could not load your profile.")
        return

    today      = datetime.now(timezone.utc).date()
    last_daily = u.get('last_daily')
    if last_daily and last_daily == today:
        tomorrow = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        diff = tomorrow - datetime.now(timezone.utc)
        h, rem = divmod(int(diff.total_seconds()), 3600)
        m = rem // 60
        await ctx.send(f"⏳ Already claimed today! Come back in **{h}h {m}m**.")
        return

    yesterday    = today - timedelta(days=1)
    streak       = (u.get('daily_streak') or 0) + 1 if last_daily == yesterday else 1
    streak_bonus = min(streak - 1, 7) * DAILY_STREAK_BONUS
    prestige_val = u.get('prestige') or 0
    total        = int((DAILY_CREDITS + streak_bonus) * prestige_multiplier(prestige_val))

    conn = db(); cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE users SET credits=credits+%s, last_daily=%s, daily_streak=%s WHERE user_id=%s",
            (total, today, streak, uid)
        )
        cur.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,'daily')", (uid, total))
        conn.commit()
    except Exception as e:
        conn.rollback()
        await ctx.send("❌ An error occurred. Try again.")
        return
    finally:
        release(conn)

    e = discord.Embed(title="📅 Daily Claim!", color=0x57F287)
    e.add_field(name="💰 Credits", value=f"**{total:,}**",        inline=True)
    e.add_field(name="🔥 Streak",  value=f"**{streak}** day(s)",   inline=True)
    if streak_bonus:
        e.add_field(name="🎁 Streak Bonus", value=f"+{streak_bonus}", inline=True)
    e.set_footer(text="Come back tomorrow! Max streak bonus at day 8.")
    await ctx.send(embed=e)

@bot.command()
async def work(ctx):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    u     = get_user(uid)
    items = get_user_items(uid)
    if not u:
        await ctx.send("❌ Could not load your profile.")
        return

    # Toggle cognitive machine
    if ctx.message.content.strip() in ('-work cognitive on', '-work cognitive off'):
        pass  # handled below via subcommand args — use -worktoggle

    now_ts     = int(time.time())
    cooldown_s = int(WORK_COOLDOWN_H * 3600)
    elapsed    = now_ts - (u.get('last_work_ts') or 0)

    if elapsed < cooldown_s:
        remaining = cooldown_s - elapsed
        h, rem = divmod(remaining, 3600)
        m = rem // 60
        s = rem % 60
        time_str = f"**{h}h {m}m**" if h > 0 else (f"**{m}m {s}s**" if m > 0 else f"**{s}s**")
        await ctx.send(f"⏳ You're tired! Work again in {time_str}.")
        return

    work_count   = u.get('work_count') or 0
    rank_name, rank_salary, rank_emoji, rank_idx = get_job_rank(work_count)
    job_title    = JOB_TITLES.get(rank_name, rank_name)
    prev_rank_min = JOB_RANKS[rank_idx][1]
    jobs_in_rank  = work_count - prev_rank_min
    within_rank_mult = min(1.0 + (jobs_in_rank * 0.02), 1.5)
    base_pay     = min(int(rank_salary * within_rank_mult), 5000)
    prestige_val = u.get('prestige') or 0
    earned       = int(base_pay * prestige_multiplier(prestige_val))
    action       = random.choice(WORK_ACTIONS)
    new_work_count = work_count + 1

    conn = db(); cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE users SET credits=credits+%s, last_work_ts=%s, work_count=%s WHERE user_id=%s",
            (earned, now_ts, new_work_count, uid)
        )
        cur.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,'work')", (uid, earned))
        conn.commit()
    except Exception as e:
        conn.rollback()
        await ctx.send("❌ An error occurred. Try again.")
        return
    finally:
        release(conn)

    new_rank_name, new_rank_salary, new_rank_emoji, new_rank_idx = get_job_rank(new_work_count)
    ranked_up = new_rank_idx > rank_idx

    # Auto-machine status note
    machine_lines = []
    if items.get('has_cognitive_machine'):
        state = "ON 🟢" if items.get('cognitive_enabled') else "OFF 🔴"
        machine_lines.append(f"🧠 Cognitive Machine: {state} (use `-worktoggle cognitive`)")
    if items.get('has_ai_machine'):
        state = "ON 🟢" if items.get('ai_enabled') else "OFF 🔴"
        machine_lines.append(f"🤖 AI Machine: {state} (use `-worktoggle ai`)")

    e = discord.Embed(title="💼 Work Complete!", color=0x57F287)
    e.description = f"{rank_emoji} **{job_title}** {ctx.author.display_name} {action} and earned **{earned:,} credits**!"
    e.add_field(name="📊 Job",  value=f"{rank_name} (#{new_work_count})", inline=True)
    e.add_field(name="💵 Pay",  value=f"{earned:,} credits",              inline=True)
    if prestige_val > 0:
        e.add_field(name="⭐ Prestige", value=f"×{prestige_multiplier(prestige_val):.1f}", inline=True)
    if machine_lines:
        e.add_field(name="⚙️ Machines", value="\n".join(machine_lines), inline=False)

    next_rank = get_next_job_rank(new_work_count)
    if next_rank:
        nxt_name, nxt_min, nxt_salary, nxt_emoji = next_rank
        jobs_needed = nxt_min - new_work_count
        e.set_footer(text=f"Next: {nxt_emoji} {nxt_name} in {jobs_needed} job(s) • CD: 30min")
    else:
        e.set_footer(text=f"MAX RANK: {rank_emoji} {rank_name} • CD: 30min")

    if ranked_up:
        new_title = JOB_TITLES.get(new_rank_name, new_rank_name)
        await ctx.send(embed=e)
        rank_e = discord.Embed(
            title=f"🎉 PROMOTION! {new_rank_emoji} {new_rank_name}",
            description=(f"Congratulations **{ctx.author.display_name}**!\n"
                         f"Promoted to **{new_title}**!\n"
                         f"New base salary: **{new_rank_salary:,} credits/job**"),
            color=0xFFD700
        )
        await ctx.send(embed=rank_e)
    else:
        await ctx.send(embed=e)

@bot.command()
async def worktoggle(ctx, machine: str = None):
    """Toggle Cognitive Machine or AI Machine on/off."""
    uid   = ctx.author.id
    ensure_user(uid, str(ctx.author))
    items = get_user_items(uid)

    if machine is None:
        await ctx.send("❌ Usage: `-worktoggle cognitive` or `-worktoggle ai`")
        return

    machine = machine.lower()
    if machine in ("cognitive", "cog"):
        if not items.get('has_cognitive_machine'):
            await ctx.send("❌ You don't own a Cognitive Machine. Buy one with `-buy cognitive_machine`.")
            return
        new_state = not items.get('cognitive_enabled', False)
        conn = db(); cur = conn.cursor()
        try:
            cur.execute("UPDATE user_items SET cognitive_enabled=%s WHERE user_id=%s", (new_state, uid))
            conn.commit()
        finally:
            release(conn)
        status = "**ON** 🟢" if new_state else "**OFF** 🔴"
        await ctx.send(f"🧠 Cognitive Machine is now {status}.\n{'Auto-working every 30 min at 0.75× salary.' if new_state else 'Auto-work paused.'}")

    elif machine in ("ai", "aimachine"):
        if not items.get('has_ai_machine'):
            await ctx.send("❌ You don't own an AI Machine. Purchase from the Black Market.")
            return
        new_state = not items.get('ai_enabled', False)
        conn = db(); cur = conn.cursor()
        try:
            cur.execute("UPDATE user_items SET ai_enabled=%s WHERE user_id=%s", (new_state, uid))
            conn.commit()
        finally:
            release(conn)
        status = "**ON** 🟢" if new_state else "**OFF** 🔴"
        await ctx.send(f"🤖 AI Machine is now {status}.\n{'Auto-working every 15 min at 2× salary.' if new_state else 'Auto-work paused.'}")
    else:
        await ctx.send("❌ Unknown machine. Use `cognitive` or `ai`.")

@bot.command()
async def items(ctx, member: discord.Member = None):
    """View owned permanent items."""
    target = member or ctx.author
    uid    = target.id
    ensure_user(uid, str(target))
    inv = get_user_items(uid)

    e = discord.Embed(title=f"🎒 {target.display_name}'s Item Inventory", color=0x5865F2)

    def tick(has): return "✅" if has else "❌"

    e.add_field(name="🛠️ Crafting Workshop",      value=tick(inv.get('has_workshop')),           inline=True)
    e.add_field(name="🧠 Cognitive Machine",       value=tick(inv.get('has_cognitive_machine')), inline=True)
    e.add_field(name="🤖 AI Machine",              value=tick(inv.get('has_ai_machine')),        inline=True)
    e.add_field(name="💾 Data Leak Generator",     value=tick(inv.get('has_dlg')),               inline=True)
    e.add_field(name="💻 Suspicious OS",           value=tick(inv.get('has_sos')),               inline=True)
    e.add_field(name="📡 Transfer System",         value=tick(inv.get('has_transfer')),          inline=True)

    machine_lines = []
    if inv.get('has_cognitive_machine'):
        state = "ON 🟢" if inv.get('cognitive_enabled') else "OFF 🔴"
        machine_lines.append(f"🧠 Cognitive: {state}")
    if inv.get('has_ai_machine'):
        state = "ON 🟢" if inv.get('ai_enabled') else "OFF 🔴"
        machine_lines.append(f"🤖 AI Machine: {state}")
    if machine_lines:
        e.add_field(name="⚙️ Machine Status", value="\n".join(machine_lines), inline=False)

    e.set_footer(text="Use -worktoggle cognitive/ai to toggle machines • Hacking items unlock -hack command")
    await ctx.send(embed=e)

@bot.command()
async def mybankid(ctx):
    """View your secret bank account ID."""
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    vb  = get_user_bank(uid)
    bank_id = vb.get('bank_id') or "Not assigned yet — use -vbank to generate."
    try:
        await ctx.author.send(f"🏦 **Your Secret Bank Account ID:**\n```\n{bank_id}\n```\n⚠️ Keep this private! Anyone with it could attempt to hack your account.")
        await ctx.send("✅ Your bank ID has been sent to your DMs.")
    except discord.Forbidden:
        await ctx.send(f"🏦 **Your Bank Account ID:**\n||`{bank_id}`||\n*(Enable DMs for better privacy)*")

# ─── Virtual Bank ─────────────────────────────────────────────────────────────
@bot.command(aliases=['vb', 'mybank'])
async def vbank(ctx):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    interest = apply_bank_interest(uid)
    vb = get_user_bank(uid)
    u  = get_user(uid)
    if not u:
        await ctx.send("❌ Could not load your profile.")
        return

    now  = datetime.now(timezone.utc)
    last = vb['last_interest']
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    next_interest = last + timedelta(minutes=BANK_INTEREST_MINS)
    diff  = next_interest - now
    secs  = max(0, int(diff.total_seconds()))
    m, s  = divmod(secs, 60)

    e = discord.Embed(title=f"🏦 {ctx.author.display_name}'s Virtual Bank", color=0x57F287)
    e.add_field(name="🏦 Bank Balance",  value=f"**{vb['balance']:,} credits**", inline=True)
    e.add_field(name="💳 Wallet",        value=f"**{u['credits']:,} credits**",  inline=True)
    e.add_field(name="📈 Interest Rate", value=f"+5% every 10 minutes",           inline=True)
    e.add_field(name="⏰ Next Interest", value=f"In **{m}m {s}s**",              inline=True)
    if interest > 0:
        e.add_field(name="✅ Interest Earned", value=f"+**{interest:,} credits**", inline=True)
    e.set_footer(text="Use -mybankid to view your secret bank ID • -deposit / -withdraw")
    await ctx.send(embed=e)

@bot.command()
async def deposit(ctx, amount: str):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    u   = get_user(uid)
    if not u:
        await ctx.send("❌ Could not load your profile.")
        return

    amt = u['credits'] if amount.lower() == 'all' else (int(amount) if amount.isdigit() else None)
    if amt is None:
        await ctx.send("❌ Invalid amount."); return
    if amt <= 0:
        await ctx.send("❌ Amount must be positive."); return
    if amt > u['credits']:
        await ctx.send(f"❌ You have **{u['credits']:,}** credits."); return

    apply_bank_interest(uid)
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s", (amt, uid))
        cur.execute("""
            INSERT INTO user_bank(user_id,balance,last_interest) VALUES(%s,%s,NOW())
            ON CONFLICT(user_id) DO UPDATE SET balance=user_bank.balance+%s
        """, (uid, amt, amt))
        conn.commit()
    except Exception as ex:
        conn.rollback(); await ctx.send("❌ Error. Try again."); return
    finally:
        release(conn)

    vb = get_user_bank(uid)
    e  = discord.Embed(title="🏦 Deposit Successful!", color=0x57F287)
    e.add_field(name="💰 Deposited",   value=f"**{amt:,} credits**",          inline=True)
    e.add_field(name="🏦 New Balance", value=f"**{vb['balance']:,} credits**", inline=True)
    e.set_footer(text="Balance grows 1.05× every 10 minutes!")
    await ctx.send(embed=e)

@bot.command()
async def withdraw(ctx, amount: str):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    apply_bank_interest(uid)
    vb = get_user_bank(uid)

    amt = vb['balance'] if amount.lower() == 'all' else (int(amount) if amount.isdigit() else None)
    if amt is None:
        await ctx.send("❌ Invalid amount."); return
    if amt <= 0:
        await ctx.send("❌ Amount must be positive."); return
    if amt > vb['balance']:
        await ctx.send(f"❌ Bank balance: **{vb['balance']:,}**."); return

    conn = db(); cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s", (amt, uid))
        cur.execute("UPDATE user_bank SET balance=balance-%s WHERE user_id=%s", (amt, uid))
        conn.commit()
    except Exception:
        conn.rollback(); await ctx.send("❌ Error. Try again."); return
    finally:
        release(conn)

    u = get_user(uid)
    e = discord.Embed(title="🏦 Withdrawal Successful!", color=0x57F287)
    e.add_field(name="💰 Withdrawn",  value=f"**{amt:,}**",          inline=True)
    e.add_field(name="💳 Wallet",     value=f"**{u['credits']:,}**",  inline=True)
    e.add_field(name="🏦 Remaining",  value=f"**{vb['balance']-amt:,}**", inline=True)
    await ctx.send(embed=e)

# ─── Black Market ─────────────────────────────────────────────────────────────
@bot.command()
async def blackmarket(ctx):
    """View the current Black Market."""
    now = datetime.now(timezone.utc)

    if not _black_market_active or (_black_market_expires and now >= _black_market_expires):
        await ctx.send("🌑 The Black Market is **not active** right now.\nIt appears randomly — check `#sjpcoins` for announcements!")
        return

    time_left = _black_market_expires - now
    mins      = int(time_left.total_seconds() // 60)

    e = discord.Embed(
        title="🌑 BLACK MARKET",
        description=f"⏳ Closes in **{mins} minutes**\nUse `-bmbuy <item_key>` to purchase.\n",
        color=0x2F3136
    )

    for key, item in BLACK_MARKET_ITEMS.items():
        stock = _black_market_stock.get(key, 0)
        if stock > 0:
            e.add_field(
                name=f"{item['emoji']} {item['name']} — {item['price']:,} cr",
                value=f"{item['desc']}\nStock: **{stock}**\nKey: `{key}`",
                inline=False
            )
        else:
            e.add_field(
                name=f"~~{item['emoji']} {item['name']}~~ — SOLD OUT",
                value=f"~~{item['desc']}~~",
                inline=False
            )
    e.set_footer(text="⚠️ Items are exclusive and extremely rare")
    await ctx.send(embed=e)

@bot.command()
async def bmbuy(ctx, item_key: str = None):
    """Buy an item from the Black Market."""
    if not item_key:
        await ctx.send("❌ Usage: `-bmbuy <item_key>` — see `-blackmarket` for keys.")
        return

    now = datetime.now(timezone.utc)
    if not _black_market_active or (_black_market_expires and now >= _black_market_expires):
        await ctx.send("❌ The Black Market is not active right now.")
        return

    item_key = item_key.lower()
    if item_key not in BLACK_MARKET_ITEMS:
        await ctx.send("❌ Unknown item. See `-blackmarket` for available items.")
        return

    stock = _black_market_stock.get(item_key, 0)
    if stock <= 0:
        await ctx.send("❌ That item is out of stock!")
        return

    item  = BLACK_MARKET_ITEMS[item_key]
    uid   = ctx.author.id
    ensure_user(uid, str(ctx.author))

    # Check if user already owns permanent items
    inv  = get_user_items(uid)
    perm_check = {
        "data_leak_generator": 'has_dlg',
        "suspicious_os":       'has_sos',
        "transfer_system":     'has_transfer',
        "ai_machine":          'has_ai_machine',
    }
    if item_key in perm_check and inv.get(perm_check[item_key]):
        await ctx.send(f"❌ You already own **{item['name']}**.")
        return

    e = discord.Embed(
        title=f"🌑 Black Market Purchase",
        description=f"{item['emoji']} **{item['name']}**\n{item['desc']}\n\nPrice: **{item['price']:,} credits**",
        color=0x2F3136
    )
    view = BlackMarketBuyView(item_key, item['price'], uid)
    await ctx.send(embed=e, view=view)

@bot.command()
async def usetracker(ctx, member: discord.Member = None):
    """Use an Identity Tracker to reveal a user's bank ID."""
    if member is None:
        await ctx.send("❌ Usage: `-usetracker @user`")
        return

    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))

    # Check if user has an identity tracker purchase
    conn = db(); cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id FROM black_market_log WHERE user_id=%s AND item_key='identity_tracker' ORDER BY bought_at DESC LIMIT 1",
            (uid,)
        )
        tracker = cur.fetchone()
        if not tracker:
            await ctx.send("❌ You don't have an **Identity Tracker**. Purchase from the Black Market.")
            return

        ensure_user(member.id, str(member))
        target_vb = get_user_bank(member.id)
        bank_id   = target_vb.get('bank_id') or "No bank ID found."

        # Delete the used tracker
        cur.execute("DELETE FROM black_market_log WHERE id=%s", (tracker['id'],))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"usetracker error: {e}")
        await ctx.send("❌ An error occurred.")
        return
    finally:
        release(conn)

    try:
        await ctx.author.send(
            f"🕵️ **Identity Tracker Result**\n"
            f"Target: **{member}**\n"
            f"Bank Account ID:\n```\n{bank_id}\n```\n"
            f"⚠️ One-time use consumed. The target has not been notified."
        )
        await ctx.send("✅ Result sent to your DMs. Identity Tracker consumed.")
    except discord.Forbidden:
        await ctx.send(f"✅ Identity Tracker used.\n🕵️ **{member}'s** Bank ID: ||`{bank_id}`||")

# ─── Hacking System ────────────────────────────────────────────────────────────
@bot.command()
async def hack(ctx, bank_id: str = None):
    """Attempt to hack a bank account. Must be used in DMs."""
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))

    # Must be in DM
    if not isinstance(ctx.channel, discord.DMChannel):
        try:
            await ctx.message.delete()
        except Exception: pass
        await ctx.send("⚠️ This command can only be used in **DMs with the bot** for security.", delete_after=10)
        return

    if not bank_id:
        await ctx.send("❌ Usage: `-hack <bank_account_id>`")
        return

    items = get_user_items(uid)

    if not items.get('has_sos'):
        await ctx.send("❌ You need a **Suspicious Operating System** to use hacking commands.")
        return

    # Find target by bank_id
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT user_id, balance FROM user_bank WHERE bank_id=%s", (bank_id,))
        target_bank = cur.fetchone()
    finally:
        release(conn)

    if not target_bank:
        await ctx.send("❌ Bank account ID not found.")
        return

    target_uid = target_bank['user_id']
    if target_uid == uid:
        await ctx.send("❌ You can't hack your own account.")
        return

    # Check for pending transfer
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM hack_transfers WHERE hacker_id=%s AND done=FALSE", (uid,))
        pending = cur.fetchone()
    finally:
        release(conn)
    if pending:
        await ctx.send("❌ You have a pending transfer in progress. Wait for it to complete before hacking again.")
        return

    fail_count  = get_hack_progress(uid, bank_id)
    base_chance = HACK_BASE_CHANCE if not items.get('has_dlg') else HACK_BASE_CHANCE
    bonus       = fail_count * 0.01 if items.get('has_dlg') else 0.0
    hack_chance = min(base_chance + bonus, HACK_MAX_CHANCE)

    embed = discord.Embed(
        title="💻 Hacking Terminal",
        description=(
            f"🎯 **Target Bank:** `{bank_id}`\n"
            f"💰 **Vault Balance:** {target_bank['balance']:,} credits\n"
            f"📊 **Current Hack Chance:** {hack_chance*100:.1f}%\n"
            f"{'🔥 ' + str(fail_count) + ' fail(s) recorded — chance increased!' if fail_count > 0 else ''}\n\n"
            f"**Stage 1/3:** {None}\n\n"
            f"You must solve **3 security challenges** to breach the vault.\n"
            f"Click below to begin."
        ),
        color=0x00FF41
    )

    hack_view_obj = HackChallengeView(uid, bank_id, target_uid, hack_chance)
    embed.description = (
        f"🎯 **Target Bank:** `{bank_id}`\n"
        f"💰 **Vault Balance:** {target_bank['balance']:,} credits\n"
        f"📊 **Hack Chance:** {hack_chance*100:.1f}%\n\n"
        f"**Stage 1/3:** {hack_view_obj.challenge_text}\n\n"
        f"Solve all 3 challenges to maximize your chance!"
    )
    stage_view = HackStageView(hack_view_obj, None)
    msg = await ctx.send(embed=embed, view=stage_view)
    # Update dm_message reference on the view
    for item in stage_view.children:
        if hasattr(item, 'hack_view'):
            item.hack_view = hack_view_obj
    stage_view.dm_message = msg
    # Update HackInputModal's dm_message by re-binding
    hack_view_obj._dm_msg = msg

@bot.command()
async def hackinventory(ctx):
    """View your hacking items and data cards."""
    uid   = ctx.author.id
    ensure_user(uid, str(ctx.author))
    items = get_user_items(uid)

    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM data_cards WHERE owner_id=%s ORDER BY created_at DESC LIMIT 10", (uid,))
        cards = cur.fetchall()
        cur.execute("SELECT * FROM hack_progress WHERE hacker_id=%s ORDER BY fail_count DESC", (uid,))
        progress = cur.fetchall()
    finally:
        release(conn)

    e = discord.Embed(title="🔓 Hacking Inventory", color=0x00FF41)

    def tick(has): return "✅" if has else "❌"
    e.add_field(name="💻 Suspicious OS",       value=tick(items.get('has_sos')),      inline=True)
    e.add_field(name="💾 Data Leak Generator", value=tick(items.get('has_dlg')),      inline=True)
    e.add_field(name="📡 Transfer System",     value=tick(items.get('has_transfer')), inline=True)

    if cards:
        card_lines = []
        for c in cards:
            card_lines.append(f"`{c['target_bank_id'][:20]}...` — {c['fail_count_snapshot']} fail(s) at time of card")
        e.add_field(name="🃏 Data Cards", value="\n".join(card_lines), inline=False)

    if progress:
        prog_lines = []
        for p in progress:
            bonus = p['fail_count'] * 1
            chance = min(HACK_BASE_CHANCE * 100 + bonus, HACK_MAX_CHANCE * 100)
            prog_lines.append(f"`{p['target_bank_id'][:25]}...` — {p['fail_count']} fail(s) → {chance:.1f}% hack chance")
        e.add_field(name="📊 Hack Progress", value="\n".join(prog_lines), inline=False)

    e.set_footer(text="Use -hack <bank_id> in DMs • -mybankid to see your own ID")
    await ctx.send(embed=e)

@bot.command()
async def hacktransfers(ctx):
    """View pending hack credit transfers."""
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM hack_transfers WHERE hacker_id=%s ORDER BY completes_at ASC", (uid,))
        rows = cur.fetchall()
    finally:
        release(conn)

    if not rows:
        await ctx.send("📡 No pending hack transfers.")
        return

    e = discord.Embed(title="📡 Hack Transfers", color=0x00FF41)
    now = datetime.now(timezone.utc)
    for r in rows:
        completes = r['completes_at']
        if completes.tzinfo is None:
            completes = completes.replace(tzinfo=timezone.utc)
        status = "✅ Completed" if r['done'] else f"⏳ Arrives <t:{int(completes.timestamp())}:R>"
        e.add_field(name=f"Transfer #{r['id']}", value=f"Amount: **{r['amount']:,} credits** | {status}", inline=False)
    await ctx.send(embed=e)

# ─── Shop & Buy ───────────────────────────────────────────────────────────────
@bot.command()
async def shop(ctx):
    crate_cost = get_dynamic_crate_cost()
    e = discord.Embed(title="🛒 CoinVault Shop", color=0xEB459E)
    e.description = f"Current crate cost: **{crate_cost} cr** (adjusts with economy)\n"
    e.add_field(name=f"`-buy crate` — {crate_cost:,} cr",     value="Open a crate (10% fee to bank)", inline=False)
    e.add_field(name=f"`-buy crate_x3` — {int(crate_cost*3*0.90):,} cr", value="3 crates (10% discount)", inline=False)
    e.add_field(name=f"`-buy crate_x5` — {int(crate_cost*5*0.84):,} cr", value="5 crates (16% discount)", inline=False)
    e.add_field(name="`-buy float_changer <coin_id>` — 320 cr", value="Re-randomize a coin's float", inline=False)
    e.add_field(name="`-buy market_trigger` — 250 cr",          value="Trigger a random market boom/bust", inline=False)
    e.add_field(name="`-buy workshop` — 2,000 cr",              value="Crafting Workshop (required for crafting)", inline=False)
    e.add_field(name="`-buy cognitive_machine` — 6,000 cr",     value="Auto-work every 30min at 0.75× salary", inline=False)
    e.add_field(name="`-buy polish <coin_id>` — 150 cr",        value="Upgrade coin status by one tier", inline=False)
    e.add_field(name="`-buy rename <coin_id> <name>` — 200 cr", value="Rename a coin (cosmetic)", inline=False)
    e.set_footer(text="Black Market items available randomly — watch #sjpcoins • AI Machine only from Black Market")
    await ctx.send(embed=e)

@bot.command()
async def buy(ctx, item: str = None, *args):
    if item is None:
        await ctx.send("❌ Usage: `-buy <item>`. See `-shop`.")
        return

    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    item = item.lower()

    # ── Crates ────────────────────────────────────────────────────────────────
    if item in ("crate", "crate_x3", "crate_x5"):
        base_unit = get_dynamic_crate_cost()
        count_map = {"crate": 1, "crate_x3": 3, "crate_x5": 5}
        disc_map  = {"crate": 1.0, "crate_x3": 0.90, "crate_x5": 0.84}
        n_per     = count_map[item]
        unit_cost = int(base_unit * n_per * disc_map[item])

        u = get_user(uid)
        if not u:
            await ctx.send("❌ Could not load your profile."); return

        repeat = 1
        if args and args[0].lower() == 'all':
            repeat = min(max(1, u['credits'] // unit_cost), 25)
            if u['credits'] < unit_cost:
                await ctx.send(f"❌ Need at least **{unit_cost:,}** credits."); return

        total_cost   = unit_cost * repeat
        total_crates = n_per * repeat

        if u['credits'] < total_cost:
            await ctx.send(f"❌ Need **{total_cost:,}** credits. You have **{u['credits']:,}**."); return

        bank_cut = max(1, int(total_cost * CRATE_FEE_PCT))
        conn = db(); cur = conn.cursor()
        try:
            cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s", (total_cost, uid))
            cur.execute("UPDATE bank SET total=total+%s WHERE id=1", (bank_cut,))
            cur.execute("INSERT INTO bank_log(source,amount) VALUES('crate_fee',%s)", (bank_cut,))
            opened = []
            for _ in range(total_crates):
                coin = generate_coin()
                cur.execute("""
                    INSERT INTO coins(owner_id,material,variant,status,float,serial,
                                      base_value,mat_mult,var_mult,sta_mult,flt_mult,
                                      ser_mult,total_mult,value)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (uid, coin['material'], coin['variant'], coin['status'], coin['float'],
                      coin['serial'], coin['base_value'], coin['mat_mult'], coin['var_mult'],
                      coin['sta_mult'], coin['flt_mult'], coin['ser_mult'], coin['total_mult'],
                      coin['value']))
                cur.execute("SELECT lastval() as id")
                coin['id'] = cur.fetchone()['id']
                opened.append(coin)
            sync_coin_count(uid, cur)
            conn.commit()
        except Exception as ex:
            conn.rollback()
            await ctx.send(f"❌ Error opening crates: {str(ex)[:100]}"); return
        finally:
            release(conn)

        credits_left = u['credits'] - total_cost

        if total_crates == 1:
            coin = opened[0]
            serial_str = str(coin['serial']).zfill(4)
            tier   = tier_emoji(coin['value'])
            rarity = coin_rarity_score(coin)
            rlabel = rarity_label(rarity)
            market_val = get_market_price(coin)
            e = discord.Embed(title=f"📦 Crate Opened! {tier}", color=0xFFD700)
            e.add_field(name="🪙 Coin", value=f"**{coin['variant']} {coin['material']} Coin** `#{serial_str}`", inline=False)
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
                f"Stored: **${coin['value']:.4f}**\n"
                f"📈 Market: **${market_val:.4f}**"
            ), inline=True)
            e.add_field(name="✨ Rarity", value=f"**{rlabel}** (score: {rarity:.2f})", inline=False)
            e.set_footer(text=f"Coin ID: #{coin['id']} • Credits left: {credits_left:,}")
            await ctx.send(embed=e)
        else:
            e = discord.Embed(title=f"📦 {total_crates} Crates Opened!", color=0xFFD700)
            lines = []
            total_val = total_mkt = total_rarity = 0.0
            best_coin = max(opened, key=lambda c: get_market_price(c))
            for c in opened:
                tier   = tier_emoji(c['value'])
                rarity = coin_rarity_score(c)
                mval   = get_market_price(c)
                total_val    += c['value']
                total_mkt    += mval
                total_rarity += rarity
                lines.append(
                    f"{tier} `#{c['id']}` **{c['variant']} {c['material']}** "
                    f"#{str(c['serial']).zfill(4)} — ${c['value']:.4f} | Mkt: ${mval:.4f} | {rarity_label(rarity)}"
                )
            e.description = "\n".join(lines)
            best_rarity = coin_rarity_score(best_coin)
            e.add_field(name="📊 Batch Summary", value=(
                f"Total Base: **${total_val:.4f}** | Market: **${total_mkt:.4f}**\n"
                f"Avg Rarity: **{total_rarity/len(opened):.2f}**\n"
                f"Best: `#{best_coin['id']}` **{best_coin['variant']} {best_coin['material']}** — Mkt: ${get_market_price(best_coin):.4f}"
            ), inline=False)
            e.set_footer(text=f"Credits left: {credits_left:,} • Bank fee: {bank_cut} cr")
            await ctx.send(embed=e)
        return

    # ── Float Changer ──────────────────────────────────────────────────────────
    if item == "float_changer":
        if not args:
            await ctx.send("❌ Usage: `-buy float_changer <coin_id>`"); return
        try:
            coin_id = int(args[0])
        except ValueError:
            await ctx.send("❌ Invalid coin ID."); return

        cost = SHOP_ITEMS['float_changer']['cost']
        u    = get_user(uid)
        if not u or u['credits'] < cost:
            bal = u['credits'] if u else 0
            await ctx.send(f"❌ Need **{cost:,} credits**. You have **{bal:,}**."); return

        conn = db(); cur = conn.cursor()
        try:
            cur.execute("SELECT * FROM coins WHERE id=%s AND owner_id=%s", (coin_id, uid))
            c = cur.fetchone()
            if not c:
                await ctx.send(f"❌ Coin #{coin_id} not in your inventory."); return

            old_float  = c['float']
            old_flt_m  = c['flt_mult']
            new_float, new_flt_mult = weighted_choice(FLOATS)
            new_total  = round(c['mat_mult'] * c['var_mult'] * c['sta_mult'] * new_flt_mult * c['ser_mult'], 4)
            new_value  = round(c['base_value'] * new_total, 4)

            cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s", (cost, uid))
            cur.execute("""
                UPDATE coins SET float=%s, flt_mult=%s, total_mult=%s, value=%s WHERE id=%s
            """, (new_float, new_flt_mult, new_total, new_value, coin_id))
            conn.commit()
        except Exception as ex:
            conn.rollback(); await ctx.send("❌ Error. Try again."); return
        finally:
            release(conn)

        arrow = "📈" if new_flt_mult > old_flt_m else "📉"
        e = discord.Embed(title="🎲 Float Changed!", color=0x57F287)
        e.description = (
            f"Coin `#{coin_id}` float re-rolled:\n"
            f"**{old_float}** (×{old_flt_m}) → {arrow} **{new_float}** (×{new_flt_mult})\n"
            f"New base value: **${new_value:.4f}**"
        )
        await ctx.send(embed=e)
        return

    # ── Market Trigger ─────────────────────────────────────────────────────────
    if item == "market_trigger":
        cost = SHOP_ITEMS['market_trigger']['cost']
        u    = get_user(uid)
        if not u or u['credits'] < cost:
            bal = u['credits'] if u else 0
            await ctx.send(f"❌ Need **{cost:,} credits**. You have **{bal:,}**."); return

        conn = db(); cur = conn.cursor()
        try:
            cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s", (cost, uid))
            conn.commit()
        finally:
            release(conn)

        event = generate_market_prices()
        if event:
            ev_type, mat, mult = event
            icon = "📈" if ev_type == "boom" else "📉"
            await ctx.send(
                f"⚡ **Market Trigger activated!** {icon} **{ev_type.upper()}** on **{mat}** ×{mult} "
                f"for {EVENT_DURATION_MINS} minutes!"
            )
        else:
            await ctx.send("⚡ **Market Trigger activated!** Prices refreshed but no event spawned this time.")
        return

    # ── Workshop ───────────────────────────────────────────────────────────────
    if item == "workshop":
        cost = SHOP_ITEMS['workshop']['cost']
        u    = get_user(uid)
        inv  = get_user_items(uid)
        if not u or u['credits'] < cost:
            bal = u['credits'] if u else 0
            await ctx.send(f"❌ Need **{cost:,} credits**. You have **{bal:,}**."); return
        if inv.get('has_workshop'):
            await ctx.send("❌ You already own a **Crafting Workshop**."); return

        conn = db(); cur = conn.cursor()
        try:
            cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s", (cost, uid))
            cur.execute("UPDATE user_items SET has_workshop=TRUE WHERE user_id=%s", (uid,))
            conn.commit()
        except Exception:
            conn.rollback(); await ctx.send("❌ Error. Try again."); return
        finally:
            release(conn)

        await ctx.send("🛠️ **Crafting Workshop** purchased! You can now craft special items. (More crafting recipes coming soon!)")
        return

    # ── Cognitive Machine ──────────────────────────────────────────────────────
    if item == "cognitive_machine":
        cost = SHOP_ITEMS['cognitive_machine']['cost']
        u    = get_user(uid)
        inv  = get_user_items(uid)
        if not u or u['credits'] < cost:
            bal = u['credits'] if u else 0
            await ctx.send(f"❌ Need **{cost:,} credits**. You have **{bal:,}**."); return
        if inv.get('has_cognitive_machine'):
            await ctx.send("❌ You already own a **Cognitive Machine**."); return

        conn = db(); cur = conn.cursor()
        try:
            cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s", (cost, uid))
            cur.execute("UPDATE user_items SET has_cognitive_machine=TRUE WHERE user_id=%s", (uid,))
            conn.commit()
        except Exception:
            conn.rollback(); await ctx.send("❌ Error. Try again."); return
        finally:
            release(conn)

        await ctx.send(
            "🧠 **Cognitive Machine** purchased!\n"
            "Auto-works every **30 minutes** at **0.75×** your normal salary.\n"
            "Toggle it on/off with `-worktoggle cognitive`."
        )
        return

    # ── Polish ─────────────────────────────────────────────────────────────────
    if item == "polish":
        if not args:
            await ctx.send("❌ Usage: `-buy polish <coin_id>`"); return
        try:
            coin_id = int(args[0])
        except ValueError:
            await ctx.send("❌ Invalid coin ID."); return

        cost = SHOP_ITEMS['polish']['cost']
        u    = get_user(uid)
        if not u or u['credits'] < cost:
            bal = u['credits'] if u else 0
            await ctx.send(f"❌ Need **{cost:,} credits**. You have **{bal:,}**."); return

        conn = db(); cur = conn.cursor()
        try:
            cur.execute("SELECT * FROM coins WHERE id=%s AND owner_id=%s", (coin_id, uid))
            c = cur.fetchone()
            if not c:
                await ctx.send(f"❌ Coin #{coin_id} not in your inventory."); return

            cur_status = c['status']
            if cur_status not in STATUS_ORDER:
                await ctx.send("❌ Can't polish this coin."); return
            idx = STATUS_ORDER.index(cur_status)
            if idx >= len(STATUS_ORDER) - 1:
                await ctx.send("❌ Already at max status (**Stunning**)."); return

            new_status   = STATUS_ORDER[idx + 1]
            new_sta_mult = next(m for n, m, _ in STATUSES if n == new_status)
            new_total    = round(c['mat_mult'] * c['var_mult'] * new_sta_mult * c['flt_mult'] * c['ser_mult'], 4)
            new_value    = round(c['base_value'] * new_total, 4)

            cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s", (cost, uid))
            cur.execute("UPDATE coins SET status=%s,sta_mult=%s,total_mult=%s,value=%s WHERE id=%s",
                        (new_status, new_sta_mult, new_total, new_value, coin_id))
            conn.commit()
        except Exception:
            conn.rollback(); await ctx.send("❌ Error. Try again."); return
        finally:
            release(conn)

        e = discord.Embed(title="✨ Coin Polished!", color=0x57F287)
        e.description = f"Coin `#{coin_id}`: **{cur_status}** → **{new_status}**\nNew value: **${new_value:.4f}**"
        await ctx.send(embed=e)
        return

    # ── Rename ─────────────────────────────────────────────────────────────────
    if item == "rename":
        if len(args) < 2:
            await ctx.send("❌ Usage: `-buy rename <coin_id> <new name>`"); return
        try:
            coin_id = int(args[0])
        except ValueError:
            await ctx.send("❌ Invalid coin ID."); return
        new_name = " ".join(args[1:])[:40]

        cost = SHOP_ITEMS['rename']['cost']
        u    = get_user(uid)
        if not u or u['credits'] < cost:
            bal = u['credits'] if u else 0
            await ctx.send(f"❌ Need **{cost:,} credits**. You have **{bal:,}**."); return

        conn = db(); cur = conn.cursor()
        try:
            cur.execute("SELECT id FROM coins WHERE id=%s AND owner_id=%s", (coin_id, uid))
            if not cur.fetchone():
                await ctx.send(f"❌ Coin #{coin_id} not in your inventory."); return
            cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s", (cost, uid))
            cur.execute("UPDATE coins SET custom_name=%s WHERE id=%s", (new_name, coin_id))
            conn.commit()
        except Exception:
            conn.rollback(); await ctx.send("❌ Error. Try again."); return
        finally:
            release(conn)

        await ctx.send(f"✅ Coin `#{coin_id}` renamed to **{new_name}**!")
        return

    await ctx.send(f"❌ Unknown item `{item}`. See `-shop`.")

# ─── Job Commands ─────────────────────────────────────────────────────────────
@bot.command()
async def jobrank(ctx, member: discord.Member = None):
    target = member or ctx.author
    uid    = target.id
    ensure_user(uid, str(target))
    u = get_user(uid)
    if not u:
        await ctx.send("❌ Could not load profile."); return

    work_count    = u.get('work_count') or 0
    rank_name, rank_salary, rank_emoji, rank_idx = get_job_rank(work_count)
    job_title     = JOB_TITLES.get(rank_name, rank_name)
    next_rank     = get_next_job_rank(work_count)
    prev_rank_min = JOB_RANKS[rank_idx][1]
    jobs_in_rank  = work_count - prev_rank_min
    within_rank_mult = min(1.0 + (jobs_in_rank * 0.02), 1.5)
    current_salary = int(rank_salary * within_rank_mult)

    e = discord.Embed(title=f"{rank_emoji} {target.display_name}'s Career", color=0x5865F2)
    e.add_field(name="🏷️ Title",          value=f"**{job_title}**",                                 inline=True)
    e.add_field(name="📊 Rank",            value=f"**{rank_name}** (Tier {rank_idx+1}/{len(JOB_RANKS)})", inline=True)
    e.add_field(name="💼 Jobs Worked",     value=f"**{work_count}**",                                inline=True)
    e.add_field(name="💵 Current Salary",  value=f"**{current_salary:,}** cr/job",                  inline=True)
    e.add_field(name="📈 In-Rank Growth",  value=f"+2% per job (×{within_rank_mult:.2f} now)",       inline=True)

    if next_rank:
        nxt_name, nxt_min, nxt_salary, nxt_emoji = next_rank
        jobs_needed     = nxt_min - work_count
        rank_total_jobs = nxt_min - prev_rank_min
        rank_done_jobs  = work_count - prev_rank_min
        bar_filled      = int((rank_done_jobs / rank_total_jobs) * 10)
        bar             = "█" * bar_filled + "░" * (10 - bar_filled)
        e.add_field(
            name=f"⬆️ Next: {nxt_emoji} {nxt_name}",
            value=f"`{bar}` {rank_done_jobs}/{rank_total_jobs}\n{jobs_needed} job(s) to go! Salary: **{nxt_salary:,}**",
            inline=False
        )
    else:
        e.add_field(name="🏆 Status", value="**MAX RANK ACHIEVED!**", inline=False)

    e.set_footer(text="Use -jobladder to see all ranks")
    await ctx.send(embed=e)

@bot.command()
async def jobladder(ctx):
    e = discord.Embed(title="💼 CoinVault Job Ladder", color=0xFFD700)
    lines = []
    for i, (name, min_wc, salary, emoji) in enumerate(JOB_RANKS):
        title     = JOB_TITLES.get(name, name)
        range_str = f"Jobs {min_wc}–{JOB_RANKS[i+1][1]-1}" if i+1 < len(JOB_RANKS) else f"Jobs {min_wc}+"
        lines.append(f"{emoji} **{name}** — *{title}*\n  └ {range_str} | Base: **{salary:,} cr/job**")
    e.description = "\n".join(lines)
    e.set_footer(text="Salary grows +2%/job within rank (max 1.5×) • Prestige multiplier also applies")
    await ctx.send(embed=e)

# ─── Gambling ─────────────────────────────────────────────────────────────────
@bot.command()
async def rob(ctx, target: discord.Member):
    if target.bot or target.id == ctx.author.id:
        await ctx.send("❌ Invalid target."); return

    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    ensure_user(target.id, str(target))
    u = get_user(uid)
    if not u:
        await ctx.send("❌ Could not load your profile."); return

    now_ts    = int(time.time())
    cooldown_s = ROB_COOLDOWN_H * 3600
    elapsed   = now_ts - (u.get('last_rob_ts') or 0)
    if elapsed < cooldown_s:
        remaining = cooldown_s - elapsed
        h, rem = divmod(remaining, 3600)
        m = rem // 60
        await ctx.send(f"⏳ Lay low! Rob again in **{h}h {m}m**."); return

    t = get_user(target.id)
    if not t or t['credits'] < 50:
        await ctx.send(f"❌ **{target.display_name}** needs at least 50 credits to rob."); return

    conn = db(); cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET last_rob_ts=%s WHERE user_id=%s", (now_ts, uid))
        if random.random() < ROB_SUCCESS_PCT:
            steal = max(1, int(t['credits'] * random.uniform(0.05, ROB_MAX_STEAL_PCT)))
            cur.execute("UPDATE users SET credits=GREATEST(0,credits-%s) WHERE user_id=%s", (steal, target.id))
            cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s", (steal, uid))
            conn.commit()
            e = discord.Embed(title="🦹 Successful Robbery!", color=discord.Color.green())
            e.description = f"You slipped **{steal:,} credits** from **{target.display_name}**!"
        else:
            fine = max(1, int((u.get('credits') or 0) * ROB_FINE_PCT))
            cur.execute("UPDATE users SET credits=GREATEST(0,credits-%s) WHERE user_id=%s", (fine, uid))
            cur.execute("UPDATE bank SET total=total+%s WHERE id=1", (fine,))
            cur.execute("INSERT INTO bank_log(source,amount) VALUES('rob_fine',%s)", (fine,))
            conn.commit()
            e = discord.Embed(title="🚔 Caught Red-Handed!", color=discord.Color.red())
            e.description = f"Caught robbing **{target.display_name}** — paid a **{fine:,} credit** fine!"
    except Exception as ex:
        conn.rollback(); await ctx.send("❌ Error. Try again."); return
    finally:
        release(conn)

    await ctx.send(embed=e)

@bot.command()
async def gamble(ctx, amount: int):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    u = get_user(uid)
    if not u:
        await ctx.send("❌ Could not load your profile."); return
    if amount < GAMBLE_MIN:
        await ctx.send(f"❌ Minimum bet: **{GAMBLE_MIN:,} credits**."); return
    if amount > u['credits']:
        await ctx.send(f"❌ You have **{u['credits']:,}** credits."); return

    e    = discord.Embed(title="🪙 Coinflip", description=f"Betting **{amount:,} credits** — pick a side!", color=0xFEE75C)
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
        await ctx.send("❌ Could not load your profile."); return
    if amount < GAMBLE_MIN:
        await ctx.send(f"❌ Minimum bet: **{GAMBLE_MIN:,}**."); return
    if amount > u['credits']:
        await ctx.send(f"❌ You have **{u['credits']:,}** credits."); return

    weights = [30, 25, 25, 15, 5, 3, 2]
    reel    = random.choices(SLOT_SYMBOLS, weights=weights, k=3)
    result_key  = tuple(reel)
    multiplier  = SLOT_PAYOUTS.get(result_key, 0)

    conn = db(); cur = conn.cursor()
    try:
        if multiplier > 0:
            winnings = amount * multiplier
            net      = winnings - amount
            cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s", (net, uid))
            bank_cut = max(1, int(amount * 0.02))
            cur.execute("UPDATE bank SET total=total+%s WHERE id=1", (bank_cut,))
            cur.execute("INSERT INTO bank_log(source,amount) VALUES('slots_tax',%s)", (bank_cut,))
            color       = discord.Color.gold()
            result_line = f"🎉 **{' | '.join(reel)}** — **{multiplier}×**! Won **{winnings:,}** (net +**{net:,}**)"
        else:
            cur.execute("UPDATE users SET credits=GREATEST(0,credits-%s) WHERE user_id=%s", (amount, uid))
            bank_cut = max(1, int(amount * 0.50))
            cur.execute("UPDATE bank SET total=total+%s WHERE id=1", (bank_cut,))
            cur.execute("INSERT INTO bank_log(source,amount) VALUES('slots_house',%s)", (bank_cut,))
            color       = discord.Color.red()
            result_line = f"💸 **{' | '.join(reel)}** — No match. Lost **{amount:,}**."
        conn.commit()
    except Exception:
        conn.rollback(); await ctx.send("❌ Error. Try again."); return
    finally:
        release(conn)

    e = discord.Embed(title="🎰 Slot Machine", description=result_line, color=color)
    e.set_footer(text="3×🎰=50x | 3×💎=20x | 3×🌟=15x | 3×🍇=8x | ...")
    await ctx.send(embed=e)

@bot.command()
async def prestige(ctx):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    u = get_user(uid)
    if not u:
        await ctx.send("❌ Could not load your profile."); return
    if u['credits'] < PRESTIGE_COST:
        await ctx.send(f"❌ Prestige costs **{PRESTIGE_COST:,}** credits. You have **{u['credits']:,}**."); return

    conn = db(); cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET credits=credits-%s,prestige=prestige+1 WHERE user_id=%s", (PRESTIGE_COST, uid))
        cur.execute("UPDATE bank SET total=total+%s WHERE id=1", (PRESTIGE_COST // 2,))
        cur.execute("INSERT INTO bank_log(source,amount) VALUES('prestige',%s)", (PRESTIGE_COST // 2,))
        conn.commit()
    except Exception:
        conn.rollback(); await ctx.send("❌ Error. Try again."); return
    finally:
        release(conn)

    new_prestige = (u.get('prestige') or 0) + 1
    new_mult     = prestige_multiplier(new_prestige)
    e = discord.Embed(title="⭐ PRESTIGE UNLOCKED!", color=0xFFD700)
    e.description = (f"**{ctx.author.display_name}** reached **Prestige {new_prestige}**!\n"
                     f"All earnings now **×{new_mult:.1f}** permanently.")
    e.set_footer(text=f"Cost: {PRESTIGE_COST:,} cr • Half went to the treasury")
    await ctx.send(embed=e)

@bot.command(aliases=['cd'])
async def cooldown(ctx):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    u = get_user(uid)
    if not u:
        await ctx.send("❌ Could not load your profile."); return

    now_ts = int(time.time())
    now_dt = datetime.now(timezone.utc)

    def fmt_remaining(last_ts, cooldown_h):
        elapsed   = now_ts - (last_ts or 0)
        remaining = int(cooldown_h * 3600) - elapsed
        if remaining <= 0: return "✅ Ready!"
        h, rem = divmod(remaining, 3600)
        m = rem // 60; s = rem % 60
        if h > 0: return f"⏳ {h}h {m}m"
        elif m > 0: return f"⏳ {m}m {s}s"
        else: return f"⏳ {s}s"

    work_status = fmt_remaining(u.get('last_work_ts') or 0, WORK_COOLDOWN_H)
    rob_status  = fmt_remaining(u.get('last_rob_ts') or 0, ROB_COOLDOWN_H)

    today = now_dt.date()
    last_daily = u.get('last_daily')
    if not last_daily or last_daily < today:
        daily_status = "✅ Ready!"
    else:
        tomorrow = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        diff = tomorrow - now_dt
        h, rem = divmod(int(diff.total_seconds()), 3600)
        m = rem // 60
        daily_status = f"⏳ {h}h {m}m"

    if _market_prices["last_updated"]:
        next_upd = _market_prices["last_updated"] + timedelta(minutes=20)
        diff     = next_upd - now_dt
        secs     = int(diff.total_seconds())
        if secs <= 0: market_status = "🔄 Updating soon..."
        else:
            m2, s2 = divmod(secs, 60)
            market_status = f"🔄 {m2}m {s2}s"
    else:
        market_status = "🔄 Unknown"

    work_count   = u.get('work_count') or 0
    rank_name, rank_salary, rank_emoji, rank_idx = get_job_rank(work_count)
    prev_rank_min = JOB_RANKS[rank_idx][1]
    within_rank_mult = min(1.0 + (work_count - prev_rank_min) * 0.02, 1.5)
    next_pay     = int(rank_salary * within_rank_mult * prestige_multiplier(u.get('prestige') or 0))

    e = discord.Embed(title=f"⏱️ {ctx.author.display_name}'s Cooldowns", color=0x5865F2)
    e.add_field(name="📅 Daily",         value=daily_status,  inline=True)
    e.add_field(name="💼 Work",          value=work_status,   inline=True)
    e.add_field(name="🦹 Rob",           value=rob_status,    inline=True)
    e.add_field(name="🎰 Gamble/Slots",  value="✅ No CD",    inline=True)
    e.add_field(name="📈 Market Prices", value=market_status, inline=True)
    e.add_field(name=f"{rank_emoji} Next Pay", value=f"~{next_pay:,} cr ({rank_name})", inline=True)

    items = get_user_items(uid)
    if items.get('has_cognitive_machine'):
        last_cog = items.get('last_cognitive_ts') or 0
        elapsed  = now_ts - last_cog
        if elapsed >= 1800:
            cog_status = "✅ Ready (auto)"
        else:
            r = 1800 - elapsed
            cog_status = f"⏳ {r//60}m {r%60}s"
        e.add_field(name="🧠 Cognitive Machine", value=cog_status, inline=True)
    if items.get('has_ai_machine'):
        last_ai = items.get('last_ai_ts') or 0
        elapsed = now_ts - last_ai
        if elapsed >= 900:
            ai_status = "✅ Ready (auto)"
        else:
            r = 900 - elapsed
            ai_status = f"⏳ {r//60}m {r%60}s"
        e.add_field(name="🤖 AI Machine", value=ai_status, inline=True)

    e.set_footer(text="Work: 30min • Rob: 6h • Machines: auto")
    await ctx.send(embed=e)

# ─── Market Prices ─────────────────────────────────────────────────────────────
@bot.command()
async def prices(ctx, category: str = "all"):
    category = category.lower()

    if _market_prices["last_updated"]:
        now_dt      = datetime.now(timezone.utc)
        next_update = _market_prices["last_updated"] + timedelta(minutes=20)
        diff        = next_update - now_dt
        secs        = int(diff.total_seconds())
        if secs > 0:
            m, s = divmod(secs, 60)
            next_str = f"Next update in **{m}m {s}s**"
        else:
            next_str = "Updating soon..."
        last_str = _market_prices["last_updated"].strftime("%H:%M UTC")
    else:
        next_str = "Unknown"; last_str = "Not yet set"

    if category in ("material", "materials", "mat"):
        e = discord.Embed(title="📊 Live Material Market Prices", color=0xFFD700)
        e.description = f"Updated: **{last_str}** • {next_str}\n\n"
        lines = []
        for mat, (lo, hi) in MATERIAL_PRICE_RANGES.items():
            current = _market_prices["materials"].get(mat, 0)
            mid     = (lo + hi) / 2
            arrow   = "📈" if current > mid else "📉"
            sd      = get_supply_demand_mult(mat)
            ev      = get_event_mult(mat)
            tags    = []
            if ev > 1.0:  tags.append(f"🚀BOOM ×{ev}")
            elif ev < 1.0:tags.append(f"💥BUST ×{ev}")
            if sd < 0.95: tags.append("📦oversupply")
            elif sd > 1.05:tags.append("🔥scarce")
            tag_str = " " + " ".join(tags) if tags else ""
            lines.append(f"{arrow} **{mat}**: `{current:.4f}×` *(range: {lo}–{hi}×)*{tag_str}")
        e.description += "\n".join(lines)
        e.set_footer(text="-economy for full overview")
        await ctx.send(embed=e)

    elif category in ("float", "floats", "flt"):
        e = discord.Embed(title="📊 Live Float Market Prices", color=0x5865F2)
        e.description = f"Updated: **{last_str}** • {next_str}\n\n"
        lines = []
        for flt, (lo, hi) in FLOAT_PRICE_RANGES.items():
            current = _market_prices["floats"].get(flt, 0)
            mid     = (lo + hi) / 2
            arrow   = "📈" if current > mid else "📉"
            lines.append(f"{arrow} **{flt}**: `{current:.4f}×` *(range: {lo}–{hi}×)*")
        e.description += "\n".join(lines)
        await ctx.send(embed=e)

    elif category in ("status", "statuses", "sta", "condition"):
        e = discord.Embed(title="📊 Live Status Market Prices", color=0xEB459E)
        e.description = f"Updated: **{last_str}** • {next_str}\n\n"
        lines = []
        for sta, (lo, hi) in STATUS_PRICE_RANGES.items():
            current = _market_prices["statuses"].get(sta, 1.0)
            mid     = (lo + hi) / 2
            arrow   = "📈" if current > mid else "📉"
            note    = " *(30d bonus: 2×–30×)*" if sta == "Old" else ""
            lines.append(f"{arrow} **{sta}**: `{current:.4f}×` *(range: {lo}–{hi}×)*{note}")
        e.description += "\n".join(lines)
        e.set_footer(text="'Old' coins 30+ days old get special 2×–30× bonus!")
        await ctx.send(embed=e)

    else:
        e = discord.Embed(title="📈 Live Market Prices", color=0xEB459E)
        e.description = f"Last updated: **{last_str}** • {next_str}"

        mat_lines = []
        for mat, (lo, hi) in MATERIAL_PRICE_RANGES.items():
            current = _market_prices["materials"].get(mat, 0)
            arrow   = "📈" if current > (lo+hi)/2 else "📉"
            ev      = get_event_mult(mat)
            tag     = " 🚀" if ev > 1.0 else (" 💥" if ev < 1.0 else "")
            mat_lines.append(f"{arrow} **{mat}**: `{current:.3f}×`{tag}")
        e.add_field(name="🪨 Materials", value="\n".join(mat_lines), inline=True)

        flt_lines = []
        for flt, (lo, hi) in FLOAT_PRICE_RANGES.items():
            current = _market_prices["floats"].get(flt, 0)
            arrow   = "📈" if current > (lo+hi)/2 else "📉"
            flt_lines.append(f"{arrow} **{flt}**: `{current:.3f}×`")
        e.add_field(name="🌊 Floats", value="\n".join(flt_lines), inline=True)

        sta_lines = []
        for sta, (lo, hi) in STATUS_PRICE_RANGES.items():
            current = _market_prices["statuses"].get(sta, 1.0)
            arrow   = "📈" if current > (lo+hi)/2 else "📉"
            sta_lines.append(f"{arrow} **{sta}**: `{current:.3f}×`")
        e.add_field(name="🏷️ Statuses", value="\n".join(sta_lines), inline=True)

        e.set_footer(text="Use -prices material / float / status for details • 🚀=boom • 💥=bust • -economy for full overview")
        await ctx.send(embed=e)

@bot.command()
async def coinprice(ctx, coin_id: int):
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM coins WHERE id=%s", (coin_id,))
        c = cur.fetchone()
    finally:
        release(conn)
    if not c:
        await ctx.send(f"❌ Coin #{coin_id} not found."); return

    market_val = get_market_price(c)
    base_val   = c['value']
    diff       = market_val - base_val
    diff_pct   = (diff / base_val * 100) if base_val > 0 else 0
    arrow      = "📈" if diff >= 0 else "📉"
    mat_live   = _market_prices["materials"].get(c['material'], c['mat_mult'])
    flt_live   = _market_prices["floats"].get(c['float'], c['flt_mult'])
    sta_live   = get_status_market_mult(c)
    ev_mult    = get_event_mult(c['material'])
    sd_mult    = get_supply_demand_mult(c['material'])
    tier       = tier_emoji(market_val)
    name       = coin_name(c)
    serial_str = str(c['serial']).zfill(4)
    rap        = get_coin_rap(coin_id)
    trade_hist = get_coin_trade_history(coin_id)

    e = discord.Embed(title=f"{tier} Live Price — {name} #{serial_str}", color=0xFFD700)
    e.add_field(name="📦 Base Value",   value=f"${base_val:.4f}",          inline=True)
    e.add_field(name="📈 Market Value", value=f"**${market_val:.4f}**",    inline=True)
    e.add_field(name=f"{arrow} Change", value=f"{'+' if diff>=0 else ''}{diff:.4f} ({diff_pct:+.1f}%)", inline=True)
    e.add_field(name="🪨 Mat",  value=f"`{mat_live:.4f}×`",               inline=True)
    e.add_field(name="🌊 Float",value=f"`{flt_live:.4f}×` ({c['float']})", inline=True)
    e.add_field(name="🏷️ Stat", value=f"`{sta_live:.4f}×` ({c['status']})",inline=True)

    econ_lines = []
    if ev_mult != 1.0:
        ev = _market_events.get(c['material'])
        ev_type = ev['type'].upper() if ev else "EVENT"
        econ_lines.append(f"{'🚀' if ev_mult > 1.0 else '💥'} **{ev_type}**: ×{ev_mult}")
    if abs(sd_mult - 1.0) > 0.01:
        direction = "oversupply 📦" if sd_mult < 1.0 else "scarce 🔥"
        econ_lines.append(f"Supply/Demand ({direction}): ×{sd_mult:.4f}")
    if econ_lines:
        e.add_field(name="🌐 Economy Factors", value="\n".join(econ_lines), inline=False)

    rap_val = rap if rap is not None else "No trades yet"
    e.add_field(name="💹 RAP", value=f"**{rap_val}**" if rap else "No trades yet", inline=True)

    if trade_hist:
        lines = []
        for t in trade_hist:
            ts = t['traded_at']
            if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
            lines.append(f"`{t['price']:,} cr` — {t.get('seller_name','?')} → {t.get('buyer_name','?')} <t:{int(ts.timestamp())}:R>")
        e.add_field(name="🔄 Recent Trades", value="\n".join(lines), inline=False)

    if _market_prices["last_updated"]:
        now_dt      = datetime.now(timezone.utc)
        next_update = _market_prices["last_updated"] + timedelta(minutes=20)
        diff_t      = next_update - now_dt
        secs        = int(diff_t.total_seconds())
        m2, s2      = divmod(max(0, secs), 60)
        e.set_footer(text=f"Prices refresh in {m2}m {s2}s")
    await ctx.send(embed=e)

@bot.command()
async def economy(ctx):
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) as c FROM users")
        total_users = cur.fetchone()['c'] or 0
        cur.execute("SELECT COUNT(*) as c FROM coins")
        total_coins = cur.fetchone()['c'] or 0
        cur.execute("SELECT COALESCE(SUM(value),0) as v FROM coins")
        total_coin_value = float(cur.fetchone()['v'] or 0)
        cur.execute("SELECT COALESCE(SUM(credits),0) as c FROM users")
        total_wallet = int(cur.fetchone()['c'] or 0)
        cur.execute("SELECT COALESCE(SUM(balance),0) as b FROM user_bank")
        total_bank = int(cur.fetchone()['b'] or 0)
        cur.execute("SELECT total FROM bank WHERE id=1")
        treasury = int(cur.fetchone()['total'] or 0)
        cur.execute("SELECT COALESCE(SUM(amount),0) as s FROM bank_log WHERE amount<0")
        total_sunk = abs(int(cur.fetchone()['s'] or 0))
        cur.execute("SELECT material,total_sold FROM material_sales_log ORDER BY total_sold DESC LIMIT 5")
        top_sold = cur.fetchall()
    except Exception as ex:
        print(f"economy error: {ex}")
        await ctx.send("❌ Could not load economy data."); return
    finally:
        release(conn)

    crate_cost   = get_dynamic_crate_cost()
    total_credits = total_wallet + total_bank + treasury

    e = discord.Embed(title="🌐 CoinVault Economy Overview", color=0x57F287)
    e.add_field(name="💹 Economy Scale", value=(
        f"Users: **{total_users:,}**\n"
        f"Coins in circulation: **{total_coins:,}**\n"
        f"Total portfolio value: **${total_coin_value:,.2f}**\n"
        f"Credits in system: **{total_credits:,}**"
    ), inline=False)
    e.add_field(name="💸 Credit Distribution", value=(
        f"Wallets: **{total_wallet:,}**\nVirtual banks: **{total_bank:,}**\nTreasury: **{treasury:,}**"
    ), inline=True)
    e.add_field(name="🕳️ Sinks (burned)", value=(
        f"Total removed: **{total_sunk:,}**\nPrevents hyperinflation"
    ), inline=True)
    inflation_pct = round((crate_cost / CRATE_COST - 1) * 100, 1)
    e.add_field(name="📦 Inflation", value=(
        f"Base crate: **{CRATE_COST} cr**\nCurrent: **{crate_cost} cr** (+{inflation_pct}%)\nCap: **{MAX_CRATE_COST} cr**"
    ), inline=False)

    decay_supply_counters()
    supply_lines = []
    for mat, supply in sorted(_supply_counters.items(), key=lambda x: -x[1])[:8]:
        sd = get_supply_demand_mult(mat)
        tag = f"📦 oversupply (×{sd:.3f})" if sd < 0.98 else (f"🔥 scarce (×{sd:.3f})" if sd > 1.02 else "⚖️ balanced")
        supply_lines.append(f"**{mat}**: {tag}")
    if supply_lines:
        e.add_field(name="⚖️ Supply Pressure", value="\n".join(supply_lines), inline=False)

    now = datetime.now(timezone.utc)
    active_events = [(m, ev) for m, ev in _market_events.items() if now < ev['expires']]
    if active_events:
        ev_lines = []
        for mat, ev in active_events:
            mins = int((ev['expires'] - now).total_seconds() // 60)
            icon = "🚀" if ev['type'] == "boom" else "💥"
            ev_lines.append(f"{icon} **{mat}** {ev['type'].upper()} ×{ev['mult']} — {mins}m left")
        e.add_field(name="⚡ Active Events", value="\n".join(ev_lines), inline=False)
    else:
        e.add_field(name="⚡ Market Events", value="No active events.", inline=False)

    if top_sold:
        e.add_field(name="🏆 Most Sold Materials", value="\n".join(f"**{r['material']}**: {r['total_sold']:,}" for r in top_sold), inline=False)

    e.set_footer(text="-marketevents for event details")
    await ctx.send(embed=e)

@bot.command()
async def marketevents(ctx):
    now    = datetime.now(timezone.utc)
    active = [(m, ev) for m, ev in _market_events.items() if now < ev['expires']]
    if not active:
        await ctx.send("📊 No active market events right now."); return

    e = discord.Embed(title="⚡ Active Market Events", color=0xFFD700)
    for mat, ev in active:
        remaining = ev['expires'] - now
        mins = int(remaining.total_seconds() // 60)
        secs = int(remaining.total_seconds() % 60)
        icon = "🚀" if ev['type'] == "boom" else "💥"
        current_price = _market_prices["materials"].get(mat, "?")
        lo, hi = MATERIAL_PRICE_RANGES[mat]
        e.add_field(
            name=f"{icon} {mat} — {ev['type'].upper()}",
            value=(f"Multiplier: **×{ev['mult']}**\nCurrent: **{current_price:.4f}×**\n"
                   f"Normal: {lo}–{hi}×\nExpires: **{mins}m {secs}s**"),
            inline=True
        )
    e.set_footer(text="Boom = sell • Bust = hold/buy dip")
    await ctx.send(embed=e)

# ─── Inventory ─────────────────────────────────────────────────────────────────
@bot.command(aliases=['inv'])
async def inventory(ctx, *args):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))

    target_member = None
    page = 1
    for arg in args:
        if arg.startswith('<@') and arg.endswith('>'):
            mid_str = arg.strip('<@!>').strip('>')
            try:
                mid = int(mid_str)
                target_member = ctx.guild.get_member(mid) if ctx.guild else None
                if not target_member:
                    try: target_member = await bot.fetch_user(mid)
                    except Exception: pass
            except ValueError: pass
        else:
            try: page = int(arg)
            except ValueError: pass

    if target_member and target_member.id != uid:
        tu = get_user(target_member.id)
        if not tu:
            await ctx.send("❌ User not found."); return
        if not tu.get('inventory_public', False):
            await ctx.send(f"🔒 **{target_member.display_name}'s** inventory is private."); return
        view_uid, view_name = target_member.id, target_member.display_name
    else:
        view_uid, view_name = uid, ctx.author.display_name

    per_page = 8
    offset   = (page - 1) * per_page
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) as c FROM coins WHERE owner_id=%s", (view_uid,))
        total = cur.fetchone()['c']
        cur.execute("SELECT * FROM coins WHERE owner_id=%s ORDER BY value DESC LIMIT %s OFFSET %s",
                    (view_uid, per_page, offset))
        coins = cur.fetchall()
    except Exception:
        await ctx.send("❌ Error loading inventory."); return
    finally:
        release(conn)

    if not coins:
        owner_str = "Your" if view_uid == uid else f"{view_name}'s"
        await ctx.send(f"🎒 {owner_str} inventory is {'empty' if page == 1 else f'empty on page {page}'}!")
        return

    pages = max(1, math.ceil(total / per_page))
    e = discord.Embed(title=f"🎒 {view_name}'s Inventory", color=0x5865F2)
    lines = []
    for c in coins:
        serial_str = str(c['serial']).zfill(4)
        name   = c.get('custom_name') or f"{c['variant']} {c['material']} Coin"
        mkt    = get_market_price(c)
        tier   = tier_emoji(mkt)
        rap    = get_coin_rap(c['id'])
        rap_str = f" | RAP: {rap:,.0f}cr" if rap else " | Raw"
        ev     = get_event_mult(c['material'])
        ev_tag = " 🚀" if ev > 1.0 else (" 💥" if ev < 1.0 else "")
        lines.append(
            f"{tier} `#{c['id']}` **{name}** #{serial_str}\n"
            f"  {c['status']} | {c['float']} | Base: **${c['value']:.4f}** | 📈 Mkt: **${mkt:.4f}**{ev_tag}{rap_str}"
        )
    e.description = f"Page **{page}/{pages}** | Total: **{total}** coins\n\n" + "\n\n".join(lines)
    e.set_footer(text=f"-coin <id> for details • -inventory {page+1} for next page")
    await ctx.send(embed=e)

@bot.command()
async def coin(ctx, coin_id: int):
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT c.*, u.username FROM coins c JOIN users u ON u.user_id=c.owner_id WHERE c.id=%s", (coin_id,))
        c = cur.fetchone()
    except Exception:
        await ctx.send("❌ Error. Try again."); return
    finally:
        release(conn)

    if not c:
        await ctx.send(f"❌ Coin #{coin_id} not found."); return

    serial_str   = str(c['serial']).zfill(4)
    market_val   = get_market_price(c)
    tier         = tier_emoji(market_val)
    display_name = c.get('custom_name') or f"{c['variant']} {c['material']} Coin"
    rap          = get_coin_rap(coin_id)
    trade_hist   = get_coin_trade_history(coin_id)
    rarity       = coin_rarity_score(c)
    rlabel       = rarity_label(rarity)
    ev_mult      = get_event_mult(c['material'])
    sd_mult      = get_supply_demand_mult(c['material'])

    # Use live market prices for display
    mat_live = _market_prices["materials"].get(c['material'], c['mat_mult'])
    flt_live = _market_prices["floats"].get(c['float'], c['flt_mult'])
    sta_live = get_status_market_mult(c)

    e = discord.Embed(title=f"{tier} Coin #{coin_id} — {display_name}", color=0xFFD700)
    e.add_field(name="Owner",    value=c['username'],    inline=True)
    e.add_field(name="Serial",   value=f"#{serial_str}", inline=True)
    obtained = c['obtained_at']
    e.add_field(name="Obtained", value=obtained.strftime("%Y-%m-%d") if obtained else "Unknown", inline=True)

    e.add_field(name="📊 Attributes", value=(
        f"Material: **{c['material']}** (×{c['mat_mult']}) | Live: ×{mat_live:.4f}\n"
        f"Variant: **{c['variant']}** (×{c['var_mult']})\n"
        f"Status: **{c['status']}** (×{c['sta_mult']}) | Live: ×{sta_live:.4f}\n"
        f"Float: **{c['float']}** (×{c['flt_mult']}) | Live: ×{flt_live:.4f}\n"
        f"Serial #{serial_str}: (×{c['ser_mult']})"
    ), inline=True)

    e.add_field(name="💰 Valuation", value=(
        f"Stored value: **${c['value']:.4f}**\n"
        f"**📈 Market: ${market_val:.4f}**\n"
        f"*Sell = market price*"
    ), inline=True)

    econ_notes = []
    if ev_mult != 1.0:
        ev = _market_events.get(c['material'])
        econ_notes.append(f"{'🚀 BOOM' if ev_mult > 1.0 else '💥 BUST'} ×{ev_mult}")
    if abs(sd_mult - 1.0) > 0.01:
        econ_notes.append(f"{'📦 Oversupply' if sd_mult < 1.0 else '🔥 Scarce'} ×{sd_mult:.3f}")
    if econ_notes:
        e.add_field(name="🌐 Market Conditions", value="\n".join(econ_notes), inline=True)

    e.add_field(name="✨ Rarity", value=f"**{rlabel}** (score: {rarity:.2f})", inline=True)
    e.add_field(name="💹 RAP",    value=f"**{rap:,.2f} credits**" if rap else "**Raw** (never traded)", inline=True)

    if trade_hist:
        lines = []
        for t in trade_hist:
            ts = t['traded_at']
            if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
            lines.append(f"`{t['price']:,} cr` <t:{int(ts.timestamp())}:R>")
        e.add_field(name="🔄 Recent Trades", value="\n".join(lines), inline=False)

    await ctx.send(embed=e)

@bot.command()
async def sell(ctx, coin_id: str):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    if coin_id.lower() == 'all':
        await sellall(ctx); return
    try:
        cid = int(coin_id)
    except ValueError:
        await ctx.send("❌ Usage: `-sell <coin_id>` or `-sell all`"); return

    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM coins WHERE id=%s AND owner_id=%s", (cid, uid))
        c = cur.fetchone()
        if not c:
            await ctx.send(f"❌ Coin #{cid} not found in your inventory."); return
        cur.execute("SELECT id FROM auctions WHERE coin_id=%s AND status='active'", (cid,))
        if cur.fetchone():
            await ctx.send(f"❌ Coin #{cid} is in an active auction."); return

        market_val     = get_market_price(c)
        credits_earned = coin_value_to_credits(market_val)
        cur.execute("DELETE FROM coins WHERE id=%s", (cid,))
        cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s", (credits_earned, uid))
        cur.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,'sell_coin')", (uid, credits_earned))
        sync_coin_count(uid, cur)
        conn.commit()
        record_material_sale(c['material'])
        log_material_sale_db(c['material'])
    except Exception as ex:
        conn.rollback(); await ctx.send("❌ Error. Try again."); return
    finally:
        release(conn)

    name    = c.get('custom_name') or f"{c['variant']} {c['material']} Coin"
    ev_mult = get_event_mult(c['material'])
    ev_tag  = " 🚀 (BOOM!)" if ev_mult > 1.0 else (" 💥 (BUST)" if ev_mult < 1.0 else "")
    e = discord.Embed(title="💸 Coin Sold!", color=0x57F287)
    e.description = (f"Sold **{name}** `#{str(c['serial']).zfill(4)}`\n"
                     f"Base: **${c['value']:.4f}** → Market: **${get_market_price(c):.4f}** → **{credits_earned:,} credits**{ev_tag}")
    await ctx.send(embed=e)

@bot.command()
async def sellall(ctx):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT c.* FROM coins c
            WHERE c.owner_id=%s
            AND c.id NOT IN (SELECT coin_id FROM auctions WHERE status='active')
        """, (uid,))
        coins = cur.fetchall()
        if not coins:
            await ctx.send("🎒 No sellable coins."); return

        total_credits = sum(coin_value_to_credits(get_market_price(c)) for c in coins)
        ids = [c['id'] for c in coins]
        cur.execute("DELETE FROM coins WHERE id = ANY(%s)", (ids,))
        cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s", (total_credits, uid))
        cur.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,'sellall')", (uid, total_credits))
        sync_coin_count(uid, cur)
        conn.commit()
        for c in coins:
            record_material_sale(c['material'])
            log_material_sale_db(c['material'])
    except Exception:
        conn.rollback(); await ctx.send("❌ Error. Try again."); return
    finally:
        release(conn)

    e = discord.Embed(title="💸 Sold All Coins!", color=0x57F287)
    e.description = (f"Sold **{len(coins)}** coin(s) at live market prices for **{total_credits:,} credits**.\n"
                     f"*Note: large sell volumes may push supply prices down.*")
    await ctx.send(embed=e)

@bot.command()
async def privacy(ctx, setting: str = None):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    if setting is None or setting.lower() not in ('on', 'off'):
        u = get_user(uid)
        current = u.get('inventory_public', False)
        status_str = 'public' if current else 'private'
        await ctx.send(f"🔒 Privacy is currently **{status_str}**. Use `-privacy on/off`.")
        return

    is_public = setting.lower() == 'on'
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET inventory_public=%s WHERE user_id=%s", (is_public, uid))
        conn.commit()
    finally:
        release(conn)

    if is_public:
        await ctx.send("🔓 Inventory and wallet are now **public**.")
    else:
        await ctx.send("🔒 Inventory and wallet are now **private**.")

# ─── Trading ──────────────────────────────────────────────────────────────────
@bot.command()
async def trade(ctx, member: discord.Member, *, args: str = ""):
    if member.bot or member.id == ctx.author.id:
        await ctx.send("❌ Invalid trade target."); return

    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    ensure_user(member.id, str(member))

    coin_ids = []; credits_offer = 0
    parts = args.strip().split()
    for part in parts:
        if part.lower().startswith("credits:"):
            try: credits_offer = int(part.split(":")[1])
            except Exception:
                await ctx.send("❌ Invalid credits format. Use `credits:500`"); return
        elif part:
            try: coin_ids = [int(x.strip()) for x in part.split(",") if x.strip()]
            except Exception:
                await ctx.send("❌ Invalid coin IDs."); return

    if not coin_ids and credits_offer == 0:
        await ctx.send("❌ Specify coin IDs and/or credits. E.g.: `-trade @Bob 12,15 credits:500`"); return

    if coin_ids:
        conn = db(); cur = conn.cursor()
        try:
            placeholders = ','.join(['%s'] * len(coin_ids))
            cur.execute(f"SELECT id FROM coins WHERE id IN ({placeholders}) AND owner_id=%s",
                        coin_ids + [uid])
            found = [r['id'] for r in cur.fetchall()]
        finally:
            release(conn)
        invalid = set(coin_ids) - set(found)
        if invalid:
            await ctx.send(f"❌ Coins `{invalid}` not in your inventory."); return

    if credits_offer > 0:
        u = get_user(uid)
        if not u or u['credits'] < credits_offer:
            bal = u['credits'] if u else 0
            await ctx.send(f"❌ Insufficient credits. You have **{bal:,}**."); return

    conn = db(); cur = conn.cursor()
    try:
        ids_str = ",".join(str(x) for x in coin_ids)
        cur.execute("""
            INSERT INTO trades(initiator_id,receiver_id,coin_ids,credits_offer)
            VALUES(%s,%s,%s,%s) RETURNING id
        """, (uid, member.id, ids_str, credits_offer))
        trade_id = cur.fetchone()['id']
        conn.commit()
    except Exception as ex:
        conn.rollback()
        print(f"trade insert error: {ex}")
        await ctx.send("❌ An error occurred creating trade. Try again."); return
    finally:
        release(conn)

    e = discord.Embed(title=f"🤝 Trade Offer #{trade_id}", color=0xFEE75C)
    e.description = f"**{ctx.author.display_name}** → **{member.display_name}**"
    lines = []
    if coin_ids: lines.append(f"Coins: `{', '.join('#'+str(i) for i in coin_ids)}`")
    if credits_offer > 0:
        tax  = int(round(credits_offer * TRADE_TAX_PCT))
        sink = int(round(credits_offer * ECONOMY_SINK_SELL_PCT))
        net  = credits_offer - tax - sink
        lines.append(f"Credits: **{credits_offer:,}** (receiver gets **{net:,}** after fees)")
    e.add_field(name="📤 Offer", value="\n".join(lines) or "None", inline=False)
    e.set_footer(text="Expires in 2 minutes")
    view = TradeView(trade_id, uid, member.id)
    await ctx.send(f"{member.mention}", embed=e, view=view)

@bot.command()
async def trades(ctx):
    uid = ctx.author.id
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT * FROM trades
            WHERE (initiator_id=%s OR receiver_id=%s) AND status='pending'
            ORDER BY created_at DESC LIMIT 10
        """, (uid, uid))
        rows = cur.fetchall()
    finally:
        release(conn)

    if not rows:
        await ctx.send("📭 No pending trades."); return

    e = discord.Embed(title="📋 Pending Trades", color=0xFEE75C)
    for t in rows:
        role = "Sender" if t['initiator_id'] == uid else "Receiver"
        e.add_field(name=f"Trade #{t['id']} [{role}]",
                    value=(f"Coins: `{t['coin_ids'] or 'none'}` | Credits: {t['credits_offer']:,}\n"
                           f"Created: {t['created_at'].strftime('%Y-%m-%d %H:%M')}"),
                    inline=False)
    await ctx.send(embed=e)

# ─── Auctions ─────────────────────────────────────────────────────────────────
@bot.command()
async def auction(ctx, coin_id: int, start_price: int, hours: float = 24.0):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    if start_price <= 0:
        await ctx.send("❌ Start price must be > 0."); return
    if hours < 1 or hours > 168:
        await ctx.send("❌ Duration must be 1–168 hours."); return

    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM coins WHERE id=%s AND owner_id=%s", (coin_id, uid))
        c = cur.fetchone()
        if not c:
            await ctx.send(f"❌ Coin #{coin_id} not in your inventory."); return
        cur.execute("SELECT id FROM auctions WHERE coin_id=%s AND status='active'", (coin_id,))
        if cur.fetchone():
            await ctx.send(f"❌ Coin #{coin_id} is already listed."); return

        ends_at = datetime.now(timezone.utc) + timedelta(hours=hours)
        cur.execute("INSERT INTO auctions(seller_id,coin_id,start_price,ends_at) VALUES(%s,%s,%s,%s) RETURNING id",
                    (uid, coin_id, start_price, ends_at))
        auction_id = cur.fetchone()['id']
        conn.commit()
    except Exception:
        conn.rollback(); await ctx.send("❌ Error. Try again."); return
    finally:
        release(conn)

    market_val = get_market_price(c)
    e = discord.Embed(title="🏪 Auction Listed!", color=0x57F287)
    e.description = coin_display(c)
    e.add_field(name="Starting Price",  value=f"**{start_price:,} credits**",      inline=True)
    e.add_field(name="📈 Market Value", value=f"**${market_val:.4f}**",             inline=True)
    e.add_field(name="Ends At",         value=f"<t:{int(ends_at.timestamp())}:R>",  inline=True)
    e.add_field(name="Fee", value=f"{int(MARKET_FEE_PCT*100)}% + {int(ECONOMY_SINK_SELL_PCT*100)}% sink", inline=True)
    e.set_footer(text=f"Auction ID: #{auction_id}")
    await ctx.send(embed=e)

@bot.command()
async def market(ctx, page: int = 1):
    per_page = 5
    offset   = (page - 1) * per_page
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) as c FROM auctions WHERE status='active'")
        total = cur.fetchone()['c'] or 0
        if total == 0:
            await ctx.send("🏪 No active auctions. List yours with `-auction <coin_id> <price>`."); return

        cur.execute("""
            SELECT a.*, c.material, c.variant, c.status as cond, c.float, c.serial,
                   c.base_value, c.mat_mult, c.var_mult, c.sta_mult, c.flt_mult,
                   c.ser_mult, c.total_mult, c.value as coin_val, c.custom_name, c.obtained_at,
                   u.username as seller_name
            FROM auctions a
            JOIN coins c ON c.id=a.coin_id
            JOIN users u ON u.user_id=a.seller_id
            WHERE a.status='active'
            ORDER BY a.ends_at ASC
            LIMIT %s OFFSET %s
        """, (per_page, offset))
        rows = cur.fetchall()
    except Exception:
        await ctx.send("❌ Error loading market."); return
    finally:
        release(conn)

    if not rows:
        await ctx.send(f"❌ No auctions on page {page}."); return

    pages = max(1, math.ceil(total / per_page))
    e = discord.Embed(title=f"🏪 Coin Marketplace — Page {page}/{pages}", color=0xEB459E)

    now = datetime.now(timezone.utc)
    active_events = [(m, ev) for m, ev in _market_events.items() if now < ev['expires']]
    if _market_prices["last_updated"]:
        diff = _market_prices["last_updated"] + timedelta(minutes=20) - now
        secs = max(0, int(diff.total_seconds()))
        mm, ss = divmod(secs, 60)
        ev_hint = f" | Events: {', '.join(('🚀' if ev['type']=='boom' else '💥')+m for m,ev in active_events)}" if active_events else ""
        e.description = f"**{total}** listing(s) | Prices refresh in **{mm}m {ss}s**{ev_hint}\n"
    else:
        e.description = f"**{total}** listing(s)\n"

    for a in rows:
        serial_str = str(a['serial']).zfill(4)
        market_val = get_market_price(a)
        tier       = tier_emoji(market_val)
        top_bid    = f"{a['current_bid']:,} credits" if a['current_bid'] else "No bids"
        name       = a.get('custom_name') or f"{a['variant']} {a['material']} Coin"
        ends_ts    = a['ends_at']
        if ends_ts.tzinfo is None: ends_ts = ends_ts.replace(tzinfo=timezone.utc)
        base_val   = a['coin_val']
        diff_pct   = ((market_val - base_val) / base_val * 100) if base_val > 0 else 0
        price_arrow= "📈" if diff_pct >= 0 else "📉"
        rap        = get_coin_rap(a['coin_id'])
        rap_str    = f"RAP: **{rap:,.0f} cr**" if rap else "RAP: Raw"
        ev_mult    = get_event_mult(a['material'])
        ev_tag     = " 🚀BOOM" if ev_mult > 1.0 else (" 💥BUST" if ev_mult < 1.0 else "")
        e.add_field(
            name=f"{tier} Auction #{a['id']} — {name} #{serial_str}{ev_tag}",
            value=(f"Cond: **{a['cond']}** | Float: **{a['float']}**\n"
                   f"Base: **${base_val:.4f}** | {price_arrow} Mkt: **${market_val:.4f}** ({diff_pct:+.1f}%) | {rap_str}\n"
                   f"Start: **{a['start_price']:,}** | Top Bid: **{top_bid}**\n"
                   f"Seller: {a['seller_name']} | Ends: <t:{int(ends_ts.timestamp())}:R>"),
            inline=False
        )
    e.set_footer(text="Use -bid <auction_id> to bid")
    await ctx.send(embed=e)

@bot.command()
async def bid(ctx, auction_id: int):
    ensure_user(ctx.author.id, str(ctx.author))
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM auctions WHERE id=%s AND status='active'", (auction_id,))
        a = cur.fetchone()
    finally:
        release(conn)
    if not a:
        await ctx.send(f"❌ Auction #{auction_id} not found or ended."); return
    min_bid = max(a['start_price'], (a['current_bid'] or 0) + 1)
    view    = AuctionView(auction_id)
    await ctx.send(f"💰 Bidding on Auction **#{auction_id}** | Min bid: **{min_bid:,} credits**", view=view)

@bot.command()
async def myauctions(ctx):
    uid = ctx.author.id
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT a.*, c.material, c.variant, c.serial, c.custom_name
            FROM auctions a JOIN coins c ON c.id=a.coin_id
            WHERE a.seller_id=%s AND a.status='active' ORDER BY a.ends_at ASC
        """, (uid,))
        rows = cur.fetchall()
    finally:
        release(conn)
    if not rows:
        await ctx.send("📭 You have no active listings."); return
    e = discord.Embed(title="📋 Your Active Auctions", color=0xEB459E)
    for a in rows:
        serial_str = str(a['serial']).zfill(4)
        top_bid    = f"{a['current_bid']:,} credits" if a['current_bid'] else "No bids"
        name       = a.get('custom_name') or f"{a['variant']} {a['material']} Coin"
        ends_ts    = a['ends_at']
        if ends_ts.tzinfo is None: ends_ts = ends_ts.replace(tzinfo=timezone.utc)
        ev_mult    = get_event_mult(a['material'])
        ev_tag     = " 🚀" if ev_mult > 1.0 else (" 💥" if ev_mult < 1.0 else "")
        e.add_field(name=f"Auction #{a['id']} — {name} #{serial_str}{ev_tag}",
                    value=f"Start: {a['start_price']:,} | Top Bid: {top_bid}\nEnds: <t:{int(ends_ts.timestamp())}:R>",
                    inline=False)
    await ctx.send(embed=e)

@bot.command()
async def cancelauction(ctx, auction_id: int):
    uid = ctx.author.id
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM auctions WHERE id=%s AND seller_id=%s AND status='active'", (auction_id, uid))
        a = cur.fetchone()
        if not a:
            await ctx.send(f"❌ Auction #{auction_id} not found or not yours."); return
        if a['bidder_id'] and a['current_bid']:
            cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s", (a['current_bid'], a['bidder_id']))
        cur.execute("UPDATE coins SET owner_id=%s WHERE id=%s", (uid, a['coin_id']))
        cur.execute("UPDATE auctions SET status='cancelled' WHERE id=%s", (auction_id,))
        conn.commit()
    except Exception:
        conn.rollback(); await ctx.send("❌ Error. Try again."); return
    finally:
        release(conn)
    await ctx.send(f"✅ Auction **#{auction_id}** cancelled. Coin returned.")

# ─── Social / Stats ────────────────────────────────────────────────────────────
@bot.command()
async def profile(ctx, member: discord.Member = None):
    target = member or ctx.author
    uid    = target.id
    ensure_user(uid, str(target))
    if member and member.id != ctx.author.id:
        tu = get_user(uid)
        if not tu or not tu.get('inventory_public', False):
            await ctx.send(f"🔒 **{target.display_name}'s** profile is private."); return

    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM users WHERE user_id=%s", (uid,))
        u = cur.fetchone()
        cur.execute("SELECT value FROM coins WHERE owner_id=%s ORDER BY value DESC LIMIT 1", (uid,))
        best = cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(value),0) as total FROM coins WHERE owner_id=%s", (uid,))
        total_val = float(cur.fetchone()['total'])
        cur.execute("SELECT COUNT(*) as c FROM auctions WHERE seller_id=%s AND status='sold'", (uid,))
        sales = cur.fetchone()['c']
        cur.execute("SELECT COUNT(*) as c FROM trades WHERE (initiator_id=%s OR receiver_id=%s) AND status='completed'",
                    (uid, uid))
        trades_done = cur.fetchone()['c']
    except Exception:
        await ctx.send("❌ Error loading profile."); return
    finally:
        release(conn)

    if not u:
        await ctx.send("❌ User not found."); return

    apply_bank_interest(uid)
    vb  = get_user_bank(uid)
    prestige_val = u.get('prestige') or 0
    pmult = prestige_multiplier(prestige_val)
    work_count = u.get('work_count') or 0
    rank_name, rank_salary, rank_emoji, _ = get_job_rank(work_count)
    job_title = JOB_TITLES.get(rank_name, rank_name)

    e = discord.Embed(title=f"👤 {target.display_name}'s Profile", color=0x5865F2)
    e.set_thumbnail(url=target.display_avatar.url)
    e.add_field(name="🎟️ Credits",      value=f"{u['credits']:,}",              inline=True)
    e.add_field(name="🏦 Bank",          value=f"{vb['balance']:,}",             inline=True)
    e.add_field(name="🪙 Coins",         value=str(u['total_coins']),            inline=True)
    e.add_field(name="⭐ Prestige",      value=f"{prestige_val} (×{pmult:.1f})", inline=True)
    e.add_field(name="📈 Portfolio",     value=f"${total_val:.4f}",              inline=True)
    e.add_field(name="🏆 Best Coin",     value=f"${best['value']:.4f}" if best else "None", inline=True)
    e.add_field(name=f"{rank_emoji} Job",value=f"**{job_title}**",              inline=True)
    e.add_field(name="💼 Jobs",          value=f"{work_count}",                  inline=True)
    e.add_field(name="🛒 Sales/Trades",  value=f"{sales} / {trades_done}",       inline=True)
    joined = u.get('joined_at')
    privacy_status = "🔓 Public" if u.get('inventory_public', False) else "🔒 Private"
    e.set_footer(text=f"Since {joined.strftime('%Y-%m-%d') if joined else 'Unknown'} • {privacy_status}")
    await ctx.send(embed=e)

@bot.command(aliases=['lb'])
async def leaderboard(ctx):
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT u.username, u.credits, u.total_coins, u.prestige, u.work_count,
                   COALESCE(SUM(c.value), 0) as portfolio
            FROM users u LEFT JOIN coins c ON c.owner_id=u.user_id
            GROUP BY u.user_id, u.username, u.credits, u.total_coins, u.prestige, u.work_count
            ORDER BY portfolio DESC LIMIT 10
        """)
        rows = cur.fetchall()
    finally:
        release(conn)

    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    e = discord.Embed(title="🏆 CoinVault — Top Collectors", color=0xFFD700)
    for i, r in enumerate(rows):
        prestige_val = r.get('prestige') or 0
        work_count   = r.get('work_count') or 0
        rank_name, _, rank_emoji, _ = get_job_rank(work_count)
        star = f" ⭐×{prestige_val}" if prestige_val else ""
        e.add_field(name=f"{medals[i]} {r['username']}{star}",
                    value=(f"Portfolio: **${float(r['portfolio']):.4f}** | Credits: **{r['credits']:,}** | "
                           f"Coins: **{r['total_coins']}** | {rank_emoji} {rank_name}"),
                    inline=False)
    if not rows: e.description = "No users yet!"
    await ctx.send(embed=e)

@bot.command()
async def richlist(ctx):
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT username, credits, prestige, work_count FROM users ORDER BY credits DESC LIMIT 10")
        rows = cur.fetchall()
    finally:
        release(conn)

    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    e = discord.Embed(title="💰 Credit Rich List", color=0x57F287)
    for i, r in enumerate(rows):
        prestige_val = r.get('prestige') or 0
        work_count   = r.get('work_count') or 0
        rank_name, _, rank_emoji, _ = get_job_rank(work_count)
        star = f" ⭐×{prestige_val}" if prestige_val else ""
        e.add_field(name=f"{medals[i]} {r['username']}{star}",
                    value=f"**{r['credits']:,} credits** | {rank_emoji} {rank_name}", inline=False)
    if not rows: e.description = "No users yet!"
    await ctx.send(embed=e)

@bot.command()
async def bank(ctx):
    total = get_bank()
    n     = count_users()
    share = total // n if n > 0 else 0
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM daily_log ORDER BY paid_date DESC LIMIT 1")
        last = cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(amount),0) as t FROM bank_log WHERE logged_at>NOW()-INTERVAL '24 hours' AND amount>0")
        day_income = cur.fetchone()['t']
        cur.execute("SELECT ABS(COALESCE(SUM(amount),0)) as s FROM bank_log WHERE logged_at>NOW()-INTERVAL '24 hours' AND amount<0")
        day_sunk = cur.fetchone()['s']
    finally:
        release(conn)

    e = discord.Embed(title="🏦 CoinVault Bank Treasury", color=0x57F287)
    e.add_field(name="💰 Balance",         value=f"**{total:,} credits**",    inline=True)
    e.add_field(name="👥 Users",           value=str(n),                       inline=True)
    e.add_field(name="📤 Projected Share", value=f"**{share:,} cr/user**",    inline=True)
    e.add_field(name="📈 Inflow (24h)",    value=f"{day_income:,} credits",    inline=True)
    e.add_field(name="🕳️ Sunk (24h)",     value=f"{day_sunk:,} burned",       inline=True)
    if last:
        e.add_field(name="📅 Last Payout", value=f"{last['paid_date']} — {last['amount']:,}/user", inline=True)
    e.set_footer(text="Funded by: crate fees • taxes • gambling • rob fines • prestige")
    await ctx.send(embed=e)

@bot.command()
async def stats(ctx):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM users WHERE user_id=%s", (uid,))
        u = cur.fetchone()
        if not u:
            await ctx.send("❌ User not found."); return
        safe_u = dict(u)
        for key, default in [('credits',0),('prestige',0),('total_coins',0),('daily_streak',0),
                              ('last_work_ts',0),('last_rob_ts',0),('work_count',0)]:
            if safe_u.get(key) is None: safe_u[key] = default

        cur.execute("SELECT COUNT(*) as c, COALESCE(SUM(value),0) as s FROM coins WHERE owner_id=%s", (uid,))
        cs = cur.fetchone()
        trades_done = 0
        try:
            cur.execute("SELECT COUNT(*) as c FROM trades WHERE (initiator_id=%s OR receiver_id=%s) AND status='completed'",
                        (uid, uid))
            row = cur.fetchone()
            trades_done = row['c'] if row else 0
        except Exception: conn.rollback()
        cur.execute("SELECT COALESCE(SUM(amount),0) as t FROM credit_log WHERE user_id=%s AND amount>0", (uid,))
        total_earned = cur.fetchone()['t']
        cur.execute("SELECT material, COUNT(*) as c FROM coins WHERE owner_id=%s GROUP BY material ORDER BY c DESC LIMIT 3", (uid,))
        top_mats = cur.fetchall()
    except Exception as ex:
        print(f"stats error: {ex}")
        conn.rollback()
        await ctx.send("❌ Error loading stats."); return
    finally:
        release(conn)

    apply_bank_interest(uid)
    vb = get_user_bank(uid)
    prestige_val = safe_u.get('prestige') or 0
    pmult        = prestige_multiplier(prestige_val)
    work_count   = safe_u.get('work_count') or 0
    rank_name, rank_salary, rank_emoji, rank_idx = get_job_rank(work_count)
    job_title    = JOB_TITLES.get(rank_name, rank_name)
    prev_rank_min = JOB_RANKS[rank_idx][1]
    within_rank_mult = min(1.0 + (work_count - prev_rank_min) * 0.02, 1.5)
    next_pay     = int(rank_salary * within_rank_mult * pmult)
    next_rank    = get_next_job_rank(work_count)
    rank_progress = (f"{rank_name} → {next_rank[3]} {next_rank[0]} in {next_rank[1]-work_count} job(s)"
                     if next_rank else f"MAX RANK: {rank_name}")

    e = discord.Embed(title=f"📊 Stats — {ctx.author.display_name}", color=0x5865F2)
    e.add_field(name="🎟️ Credits",        value=f"{safe_u['credits']:,}",            inline=True)
    e.add_field(name="🏦 Bank",            value=f"{vb['balance']:,}",                 inline=True)
    e.add_field(name="💹 Total Earned",    value=f"{int(total_earned):,} credits",      inline=True)
    e.add_field(name="⭐ Prestige",        value=f"{prestige_val} (×{pmult:.1f})",      inline=True)
    e.add_field(name="🪙 Coins",           value=str(int(cs['c']) if cs else 0),        inline=True)
    e.add_field(name="📈 Portfolio",       value=f"${float(cs['s']):.4f}" if cs else "$0.0000", inline=True)
    e.add_field(name=f"{rank_emoji} Job",  value=f"**{job_title}**",                   inline=True)
    e.add_field(name="💼 Work Count",      value=f"{work_count}",                       inline=True)
    e.add_field(name="💵 Next Pay",        value=f"~{next_pay:,} credits",             inline=True)
    e.add_field(name="📊 Career Progress", value=rank_progress,                          inline=False)
    e.add_field(name="🤝 Trades Done",     value=str(trades_done),                      inline=True)
    e.add_field(name="🔥 Daily Streak",    value=f"{safe_u.get('daily_streak',0)} days",inline=True)
    if top_mats:
        e.add_field(name="🏅 Top Materials",
                    value="\n".join(f"{r['material']}: {r['c']}" for r in top_mats), inline=True)
    await ctx.send(embed=e)

# ─── Admin Commands ────────────────────────────────────────────────────────────
@bot.command(aliases=['removecredits', 'deduct'])
async def rmcredits(ctx, member: discord.Member, amount: int):
    if ctx.author.id != ADMIN_ID:
        await ctx.send("❌ No permission."); return
    if amount <= 0:
        await ctx.send("❌ Amount must be positive."); return
    ensure_user(member.id, str(member))
    u = get_user(member.id)
    if not u:
        await ctx.send("❌ User not found."); return
    actual = min(amount, u['credits'])
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET credits=GREATEST(0,credits-%s) WHERE user_id=%s", (amount, member.id))
        cur.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,'admin_deduct')", (member.id, -actual))
        conn.commit()
    except Exception:
        conn.rollback(); await ctx.send("❌ Error."); return
    finally:
        release(conn)

    u_after = get_user(member.id)
    e = discord.Embed(title="🛠️ Admin: Credits Removed", color=discord.Color.orange())
    e.add_field(name="👤 User",        value=str(member),                          inline=True)
    e.add_field(name="➖ Removed",     value=f"**{actual:,}**",                     inline=True)
    e.add_field(name="💳 New Balance", value=f"**{u_after['credits']:,}**",         inline=True)
    await ctx.send(embed=e)

@bot.command()
async def setannouncechannel(ctx):
    if ctx.author.id != ADMIN_ID:
        await ctx.send("❌ No permission."); return
    global _announce_channel_id
    _announce_channel_id = ctx.channel.id
    await ctx.send(f"✅ Market announcements → **#{ctx.channel.name}**.")

@bot.command()
async def forcemarketupdate(ctx):
    if ctx.author.id != ADMIN_ID:
        await ctx.send("❌ No permission."); return
    event = generate_market_prices()
    if event:
        ev_type, mat, mult = event
        await ctx.send(f"✅ Market updated! **{ev_type.upper()}** on **{mat}** ×{mult}")
    else:
        await ctx.send("✅ Market prices updated. No event this cycle.")

@bot.command()
async def forceblackmarket(ctx):
    """Admin: Force-spawn the Black Market."""
    if ctx.author.id != ADMIN_ID:
        await ctx.send("❌ No permission."); return
    global _black_market_active, _black_market_expires
    reset_black_market_stock()
    _black_market_active  = True
    _black_market_expires = datetime.now(timezone.utc) + timedelta(hours=BLACK_MARKET_DURATION_H)
    await ctx.send(f"✅ Black Market force-spawned for {BLACK_MARKET_DURATION_H} hours.")

# ─── Run ──────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
