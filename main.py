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
from functools import lru_cache

sys.stdout.reconfigure(line_buffering=True)

# ─── Flask Keep-Alive ──────────────────────────────────────────────────────────
app = Flask('')
@app.route('/')
def home(): return "CoinVault Bot is alive!"
Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# ─── Environment ──────────────────────────────────────────────────────────────
DB_URL = os.getenv('DATABASE_URL') or exit("ERROR: DATABASE_URL missing!")
TOKEN  = os.getenv('DISCORD_TOKEN')  or exit("ERROR: DISCORD_TOKEN missing!")

# ─── Connection Pool ──────────────────────────────────────────────────────────
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
    return get_pool().getconn()

def release(conn):
    try:
        get_pool().putconn(conn)
    except Exception:
        pass

# ─── In-Memory Cache ──────────────────────────────────────────────────────────
_user_cache: dict = {}          # uid -> (user_row, ts)
_items_cache: dict = {}         # uid -> (items_row, ts)
_bank_cache: dict = {}          # uid -> (bank_row, ts)
CACHE_TTL = 15                  # seconds

def _cache_get(store: dict, key):
    entry = store.get(key)
    if entry and time.monotonic() - entry[1] < CACHE_TTL:
        return entry[0]
    return None

def _cache_set(store: dict, key, value):
    store[key] = (value, time.monotonic())

def _cache_invalidate(uid: int):
    _user_cache.pop(uid, None)
    _items_cache.pop(uid, None)
    _bank_cache.pop(uid, None)

# ─── Constants ─────────────────────────────────────────────────────────────────
CRATE_COST         = 100
CRATE_FEE_PCT      = 0.10
TRADE_TAX_PCT      = 0.05
MARKET_FEE_PCT     = 0.08
ECONOMY_SINK_PCT   = 0.05
CREDITS_PER_MSG    = 1
MSG_COOLDOWN_S     = 30
DAILY_CREDITS      = 50
DAILY_STREAK_BONUS = 5
WORK_COOLDOWN_H    = 0.5
ROB_COOLDOWN_H     = 1.0          # ← 1 hour
ROB_SUCCESS_PCT    = 0.45
ROB_MAX_STEAL_PCT  = 0.20
ROB_FINE_PCT       = 0.15
GAMBLE_MIN         = 10
PRESTIGE_COST      = 5000
BANK_INTEREST_PCT  = 0.05
BANK_INTEREST_MINS = 10
TRANSFER_DELAY_MINS = 120         # ← 2 hours (hack delay)

# Economy growth
SUPPLY_DECAY_MINS   = 60
SUPPLY_DECAY_RATE   = 0.85
DEMAND_IMPACT       = 0.15
INFLATION_RATE      = 0.0002
MAX_CRATE_COST      = 250
BOOM_CHANCE         = 0.08
BUST_CHANCE         = 0.05
BOOM_MULT           = 2.5
BUST_MULT           = 0.4
EVENT_DURATION_MINS = 40

# Black Market
BLACK_MARKET_CHANNEL_ID    = 1514515923455705178
BLACK_MARKET_CHANCE        = 0.10
BLACK_MARKET_DURATION_MIN  = 15
BLACK_MARKET_ROLE_ID       = 1515614836653031475

# Hacking
HACK_BASE_CHANCE = 0.01
HACK_MAX_CHANCE  = 0.51
HACK_MIN_EARN    = 2000
HACK_MAX_EARN    = 25000
HACK_PENALTY_PCT = 0.10

# Admin
ADMIN_ID = 920309927375933490

# ─── Job Ranks ─────────────────────────────────────────────────────────────────
JOB_RANKS = [
    ("Intern",       0,    20,   "🟤"),
    ("Junior Worker",5,    35,   "⚪"),
    ("Worker",       15,   55,   "🟡"),
    ("Senior Worker",30,   80,   "🔵"),
    ("Specialist",   55,   120,  "🟢"),
    ("Expert",       90,   175,  "🟠"),
    ("Lead",         140,  250,  "🔴"),
    ("Manager",      200,  350,  "💜"),
    ("Director",     300,  500,  "💎"),
    ("Executive",    450,  750,  "👑"),
    ("Vault Master", 650,  1200, "🌟"),
    ("Coin Legend",  900,  2000, "🌌"),
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

def get_job_rank(work_count: int):
    current = JOB_RANKS[0]; idx = 0
    for i, (name, min_wc, salary, emoji) in enumerate(JOB_RANKS):
        if work_count >= min_wc:
            current = (name, min_wc, salary, emoji); idx = i
        else:
            break
    return current[0], current[2], current[3], idx

def get_next_job_rank(work_count: int):
    _, _, _, idx = get_job_rank(work_count)
    if idx + 1 < len(JOB_RANKS):
        nxt = JOB_RANKS[idx + 1]
        return nxt[0], nxt[1], nxt[2], nxt[3]
    return None

# ─── Coin Tables ───────────────────────────────────────────────────────────────
MATERIALS = [
    ("Plastic",  0.50, 10), ("Wood",    0.75,  8), ("Stone",   1.00,  6),
    ("Bronze",   1.20,  8), ("Copper",  1.25,  9), ("Iron",    1.50, 12),
    ("Steel",    1.60, 14), ("Gold",    3.00, 30), ("Aluminum",1.75, 18),
    ("Carbon",   2.00, 20), ("Tungsten",2.25, 25), ("Obsidian",2.50, 30),
    ("Topaz",    2.30, 25), ("Diamond", 5.00,100), ("Amethyst",10.00,250),
    ("Plasma", 100.00,2500),
]
VARIANTS = [
    ("Brown",    0.75,  4), ("Gray",    1.00, 2), ("Blue",    1.50,  6),
    ("Yellow",   1.75, 10), ("Black",   1.80,12), ("White",   2.00, 15),
    ("Rainbow",  5.00, 50), ("Prismatic",10.00,200),
]
STATUSES = [
    ("Broken",   0.50,12), ("Crushed",  0.60,10), ("Oxidized", 0.75, 9),
    ("Scratched",0.80, 8), ("Old",      0.90, 8), ("Like New", 0.95, 7),
    ("Normal",   1.00, 2), ("New",      1.25, 4), ("Sleek",    1.50, 8),
    ("Shiny",    1.75,10), ("Modern",   2.00,15), ("Elegant",  2.50,20),
    ("Stunning", 2.75,25),
]
STATUS_ORDER = ["Broken","Crushed","Oxidized","Scratched","Old","Like New",
                "Normal","New","Sleek","Shiny","Modern","Elegant","Stunning"]
FLOATS = [
    ("Bad",           0.50,  15), ("Good",           1.00,   2),
    ("Great",         2.00,   4), ("Amazing",        3.00,   8),
    ("Heavenly",     15.00,  50), ("Godlike",       30.00, 100),
    ("Ascendant",    50.00, 250), ("Harmonious",    75.00, 350),
    ("Transcendence",100.00,500), ("Euphonious",   125.00, 600),
    ("Symphonious", 150.00, 750), ("Euphoric",     175.00, 850),
    ("Dimensional", 200.00,1000), ("Illusional",   250.00,2000),
]

WORK_ACTIONS = [
    "polished coins at the mint","sorted crates at the warehouse",
    "delivered a rare coin shipment","appraised coins for a collector",
    "ran the coin authentication desk","catalogued the vault archives",
    "guarded the treasury overnight","tested the coin press machine",
    "cleaned the display cases","audited the bank ledgers",
    "negotiated a bulk coin deal","inspected a rare coin collection",
    "managed vault security","oversaw the quarterly coin audit",
    "closed a major acquisition deal",
]

# ─── Market Price Ranges ───────────────────────────────────────────────────────
MATERIAL_PRICE_RANGES = {
    "Plastic":(0.4,0.9),"Wood":(0.6,0.9),"Stone":(0.8,1.5),"Bronze":(1.0,2.0),
    "Copper":(1.1,2.3),"Iron":(1.3,2.8),"Steel":(1.5,3.0),"Gold":(2.5,5.0),
    "Aluminum":(1.5,4.0),"Carbon":(1.5,3.0),"Tungsten":(2.0,5.0),
    "Obsidian":(2.3,6.0),"Topaz":(2.1,5.5),"Diamond":(5.0,10.0),
    "Amethyst":(10.0,30.0),"Plasma":(100.0,250.0),
}
FLOAT_PRICE_RANGES = {
    "Bad":(0.3,1.0),"Good":(0.7,1.3),"Great":(1.6,2.4),"Amazing":(2.5,3.5),
    "Heavenly":(10.0,30.0),"Godlike":(25.0,60.0),"Ascendant":(35.0,70.0),
    "Harmonious":(60.0,85.0),"Transcendence":(70.0,115.0),"Euphonious":(110.0,145.0),
    "Symphonious":(135.0,165.0),"Euphoric":(160.0,195.0),
    "Dimensional":(190.0,250.0),"Illusional":(230.0,275.0),
}
STATUS_PRICE_RANGES = {
    "Broken":(0.4,0.6),"Crushed":(0.5,0.7),"Oxidized":(0.5,1.0),
    "Scratched":(0.7,1.2),"Old":(0.8,2.0),"Like New":(0.8,1.2),
    "Normal":(1.0,2.0),"New":(1.25,3.0),"Sleek":(1.5,4.0),
    "Shiny":(1.75,5.0),"Modern":(2.0,6.0),"Elegant":(2.5,8.0),"Stunning":(2.75,10.0),
}

# ─── Economy State ─────────────────────────────────────────────────────────────
_market_prices = {"materials":{},"floats":{},"statuses":{},"last_updated":None}
_supply_counters   = {m: 0.0 for m in MATERIAL_PRICE_RANGES}
_supply_last_decay = time.monotonic()
_market_events: dict = {}
_user_black_markets: dict = {}
_announce_channel_id = None

# ─── Shop Items ────────────────────────────────────────────────────────────────
SHOP_ITEMS = {
    "crate":              {"cost": 100,   "desc": "Open a coin crate"},
    "crate_x3":           {"cost": 270,   "desc": "Open 3 crates (10% off)"},
    "crate_x5":           {"cost": 420,   "desc": "Open 5 crates (16% off)"},
    "float_changer":      {"cost": 320,   "desc": "Re-roll a coin's float"},
    "market_trigger":     {"cost": 250,   "desc": "Trigger a random market event"},
    "workshop":           {"cost": 2000,  "desc": "Crafting Workshop"},
    "cognitive_machine":  {"cost": 6000,  "desc": "Auto-work 30min @ 0.75× salary"},
    "ai_machine":         {"cost": 50000, "desc": "Auto-work 15min @ 2× salary (Black Market)"},
    "polish":             {"cost": 150,   "desc": "Upgrade coin status one tier"},
    "rename":             {"cost": 200,   "desc": "Rename a coin"},
}

# ─── Black Market Items ────────────────────────────────────────────────────────
BLACK_MARKET_ITEMS = {
    "identity_tracker":     {"name":"Identity Tracker",             "price":5100,  "stock":10,"chance":0.60,"desc":"Reveal a user's bank ID (one-time).","emoji":"🕵️"},
    "data_leak_generator":  {"name":"Data Leak Generator",          "price":15500, "stock":1, "chance":0.30,"desc":"Each failed hack gives +1% success chance (max 51%).","emoji":"💾"},
    "suspicious_os":        {"name":"Suspicious Operating System",  "price":41000, "stock":1, "chance":0.70,"desc":"Unlocks -hack in DMs.","emoji":"💻"},
    "transfer_system":      {"name":"Transfer System",              "price":27000, "stock":1, "chance":0.65,"desc":"Hacked funds transfer in 2 hours (safer).","emoji":"📡"},
    "ai_machine":           {"name":"AI Machine",                   "price":50000, "stock":1, "chance":0.20,"desc":"Auto-work every 15min @ 2× salary.","emoji":"🤖"},
}

# ─── Supply & Demand ──────────────────────────────────────────────────────────
def record_material_sale(material: str, qty: int = 1):
    if material in _supply_counters:
        _supply_counters[material] += qty

def decay_supply_counters():
    global _supply_last_decay
    now = time.monotonic()
    ticks = int((now - _supply_last_decay) / 60 // SUPPLY_DECAY_MINS)
    if ticks > 0:
        for m in _supply_counters:
            _supply_counters[m] *= (SUPPLY_DECAY_RATE ** ticks)
        _supply_last_decay = now

def get_supply_demand_mult(material: str) -> float:
    decay_supply_counters()
    sales = _supply_counters.get(material, 0.0)
    norm  = min(sales / 20.0, 1.0)
    return round(1.0 + DEMAND_IMPACT - norm * DEMAND_IMPACT * 2, 4)

def get_event_mult(material: str) -> float:
    now = datetime.now(timezone.utc)
    ev  = _market_events.get(material)
    if ev and now < ev["expires"]:
        return ev["mult"]
    return 1.0

def maybe_trigger_market_event():
    now = datetime.now(timezone.utc)
    for m in list(_market_events):
        if now >= _market_events[m]["expires"]:
            del _market_events[m]
    mats = list(MATERIAL_PRICE_RANGES.keys())
    if random.random() < BOOM_CHANCE:
        available = [m for m in mats if m not in _market_events]
        if available:
            mat  = random.choice(available)
            mult = round(random.uniform(1.5, BOOM_MULT), 2)
            _market_events[mat] = {"type":"boom","mult":mult,"expires":now+timedelta(minutes=EVENT_DURATION_MINS)}
            return ("boom", mat, mult)
    if random.random() < BUST_CHANCE:
        available = [m for m in mats if m not in _market_events]
        if available:
            mat  = random.choice(available)
            mult = round(random.uniform(BUST_MULT, 0.7), 2)
            _market_events[mat] = {"type":"bust","mult":mult,"expires":now+timedelta(minutes=EVENT_DURATION_MINS)}
            return ("bust", mat, mult)
    return None

def generate_market_prices():
    event = maybe_trigger_market_event()
    mat = {}
    for name,(lo,hi) in MATERIAL_PRICE_RANGES.items():
        base   = random.uniform(lo, hi)
        raw    = base * get_supply_demand_mult(name) * get_event_mult(name)
        mat[name] = round(max(lo*0.5, min(raw, hi*3.0)), 4)
    flt = {n: round(random.uniform(lo,hi),4) for n,(lo,hi) in FLOAT_PRICE_RANGES.items()}
    sta = {n: round(random.uniform(lo,hi),4) for n,(lo,hi) in STATUS_PRICE_RANGES.items()}
    _market_prices["materials"]    = mat
    _market_prices["floats"]       = flt
    _market_prices["statuses"]     = sta
    _market_prices["last_updated"] = datetime.now(timezone.utc)
    return event

def get_status_market_mult(coin_row) -> float:
    status   = coin_row.get("status","Normal")
    base_mult = _market_prices["statuses"].get(status, 1.0)
    if status == "Old":
        obtained = coin_row.get("obtained_at")
        if obtained:
            if obtained.tzinfo is None: obtained = obtained.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - obtained).days >= 30:
                base_mult = round(random.uniform(2.0, 30.0), 4)
    return base_mult

def get_market_price(coin_row) -> float:
    mat_mult = _market_prices["materials"].get(coin_row["material"], coin_row["mat_mult"])
    flt_mult = _market_prices["floats"].get(coin_row["float"],       coin_row["flt_mult"])
    sta_mult = get_status_market_mult(coin_row)
    total    = mat_mult * coin_row["var_mult"] * sta_mult * flt_mult * coin_row["ser_mult"]
    return round(coin_row["base_value"] * total, 4)

def get_dynamic_crate_cost() -> int:
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) as c FROM coins")
        total_coins = cur.fetchone()['c'] or 0
    finally:
        release(conn)
    cost = int(CRATE_COST * (1.0 + (total_coins / 500) * INFLATION_RATE * 100))
    return min(cost, MAX_CRATE_COST)

def weighted_choice(table):
    weights = [1.0/d for _,_,d in table]
    total   = sum(weights)
    r = random.uniform(0, total)
    cum = 0
    for (name,mult,_),w in zip(table,weights):
        cum += w
        if r <= cum:
            return name, mult
    return table[-1][0], table[-1][1]

def serial_multiplier(s: int):
    if s > 0 and s % 1000 == 0: return s, 10.0
    if s == 9999:                return s, 10.0
    if "999" in str(s):          return s, 10.0
    if "99"  in str(s):          return s, 10.0
    if s < 10:                   return s, 5.0
    if s < 100:                  return s, 3.0
    if s % 500 == 0 or s % 250 == 0 or s % 100 == 0: return s, 2.0
    return s, 1.0

def generate_coin():
    material, mat_mult = weighted_choice(MATERIALS)
    variant,  var_mult = weighted_choice(VARIANTS)
    status,   sta_mult = weighted_choice(STATUSES)
    float_n,  flt_mult = weighted_choice(FLOATS)
    serial_num = random.randint(0, 9999)
    _, ser_mult = serial_multiplier(serial_num)
    base_value  = round(random.uniform(1.0, 5.0), 2)
    total_mult  = mat_mult * var_mult * sta_mult * flt_mult * ser_mult
    return {
        "material":material,"variant":variant,"status":status,"float":float_n,
        "serial":serial_num,"base_value":base_value,
        "mat_mult":mat_mult,"var_mult":var_mult,"sta_mult":sta_mult,
        "flt_mult":flt_mult,"ser_mult":ser_mult,
        "total_mult":round(total_mult,4),"value":round(base_value*total_mult,4),
    }

def coin_rarity_score(c) -> float:
    return round(c.get("mat_mult",1)+c.get("var_mult",1)+c.get("sta_mult",1)+
                 c.get("flt_mult",1)+c.get("ser_mult",1),4)

def rarity_label(score: float) -> str:
    if score >= 500: return "🌌 Transcendent"
    if score >= 200: return "🌠 Mythic"
    if score >= 50:  return "👑 Legendary"
    if score >= 20:  return "💎 Epic"
    if score >= 10:  return "🔥 Rare"
    if score >= 5:   return "⭐ Uncommon"
    return "⚪ Common"

def tier_emoji(v: float):
    if v >= 5000: return "🌌"
    if v >= 1000: return "🌠"
    if v >= 500:  return "👑"
    if v >= 100:  return "💎"
    if v >= 50:   return "🔥"
    if v >= 20:   return "⭐"
    if v >= 10:   return "🟡"
    if v >= 5:    return "🔵"
    if v >= 2:    return "⚪"
    return "🟤"

def coin_name(c):
    return c.get('custom_name') or f"{c['variant']} {c['material']} Coin"

def coin_value_to_credits(v: float) -> int:
    return max(1, int(v))

def prestige_multiplier(p: int) -> float:
    return 1.0 + p * 0.1

def generate_bank_id() -> str:
    import string
    chars = string.ascii_uppercase + string.digits
    seg   = lambda n: ''.join(random.choices(chars, k=n))
    return f"CVB-{seg(4)}-{seg(6)}-{seg(4)}-{seg(8)}"

# ─── Black Market Helpers ──────────────────────────────────────────────────────
def _generate_bm_stock():
    return {k: item["stock"] for k,item in BLACK_MARKET_ITEMS.items() if random.random() < item["chance"]}

def get_user_bm(uid: int):
    bm = _user_black_markets.get(uid)
    if not bm: return None
    if datetime.now(timezone.utc) >= bm["expires"]:
        del _user_black_markets[uid]; return None
    return bm

def spawn_user_bm(uid: int):
    bm = {"active":True,"expires":datetime.now(timezone.utc)+timedelta(minutes=BLACK_MARKET_DURATION_MIN),
          "stock":_generate_bm_stock()}
    _user_black_markets[uid] = bm
    return bm

# ─── Role Check ────────────────────────────────────────────────────────────────
async def has_black_market_role(user) -> bool:
    for guild in bot.guilds:
        member = guild.get_member(user.id)
        if member and any(r.id == BLACK_MARKET_ROLE_ID for r in member.roles):
            return True
    return False

# ─── DB Init ──────────────────────────────────────────────────────────────────
def init_db():
    conn = db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id          BIGINT PRIMARY KEY,
            username         TEXT,
            credits          BIGINT DEFAULT 0,
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
            id         SERIAL PRIMARY KEY,
            coin_id    INT,
            seller_id  BIGINT,
            buyer_id   BIGINT,
            price      INT NOT NULL,
            traded_at  TIMESTAMP DEFAULT NOW()
        )
    """)
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
            id          SERIAL PRIMARY KEY,
            seller_id   BIGINT,
            coin_id     INT,
            start_price INT,
            current_bid INT DEFAULT 0,
            bidder_id   BIGINT,
            ends_at     TIMESTAMP,
            status      TEXT DEFAULT 'active',
            created_at  TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_items (
            user_id               BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            has_workshop          BOOLEAN DEFAULT FALSE,
            has_cognitive_machine BOOLEAN DEFAULT FALSE,
            cognitive_enabled     BOOLEAN DEFAULT FALSE,
            last_cognitive_ts     BIGINT DEFAULT 0,
            has_ai_machine        BOOLEAN DEFAULT FALSE,
            ai_enabled            BOOLEAN DEFAULT FALSE,
            last_ai_ts            BIGINT DEFAULT 0,
            has_dlg               BOOLEAN DEFAULT FALSE,
            has_sos               BOOLEAN DEFAULT FALSE,
            has_transfer          BOOLEAN DEFAULT FALSE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS hack_progress (
            id             SERIAL PRIMARY KEY,
            hacker_id      BIGINT,
            target_bank_id TEXT,
            fail_count     INT DEFAULT 0,
            updated_at     TIMESTAMP DEFAULT NOW(),
            UNIQUE(hacker_id, target_bank_id)
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS black_market_log (
            id        SERIAL PRIMARY KEY,
            user_id   BIGINT,
            item_key  TEXT,
            price     INT,
            bought_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS data_cards (
            id                  SERIAL PRIMARY KEY,
            owner_id            BIGINT,
            target_bank_id      TEXT,
            fail_count_snapshot INT,
            created_at          TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bank (
            id    INT PRIMARY KEY DEFAULT 1,
            total BIGINT DEFAULT 0
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS material_sales_log (
            material   TEXT PRIMARY KEY,
            total_sold INT DEFAULT 0,
            last_reset TIMESTAMP DEFAULT NOW()
        )
    """)
    for mat in MATERIAL_PRICE_RANGES:
        cur.execute("INSERT INTO material_sales_log(material,total_sold) VALUES(%s,0) ON CONFLICT(material) DO NOTHING",(mat,))
    # Indexes for speed
    cur.execute("CREATE INDEX IF NOT EXISTS idx_coins_owner ON coins(owner_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_coins_owner_val ON coins(owner_id, value DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_auctions_status ON auctions(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_coin_trades_coin ON coin_trades(coin_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_hack_transfers ON hack_transfers(hacker_id, done)")
    conn.commit()
    print("✅ DB initialized!")
    release(conn)

# ─── DB Helpers ───────────────────────────────────────────────────────────────
MSG_COOLDOWNS: dict = {}

def ensure_user(uid: int, username: str):
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO users(user_id,username,credits,last_msg_ts,daily_streak,last_work_ts,
                              last_rob_ts,prestige,total_coins,work_count,inventory_public)
            VALUES(%s,%s,0,0,0,0,0,0,0,0,FALSE)
            ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username
        """, (uid, username))
        cur.execute("INSERT INTO user_bank(user_id,balance,last_interest) VALUES(%s,0,NOW()) ON CONFLICT(user_id) DO NOTHING",(uid,))
        cur.execute("SELECT bank_id FROM user_bank WHERE user_id=%s",(uid,))
        row = cur.fetchone()
        if row and not row['bank_id']:
            cur.execute("UPDATE user_bank SET bank_id=%s WHERE user_id=%s",(generate_bank_id(),uid))
        cur.execute("INSERT INTO user_items(user_id) VALUES(%s) ON CONFLICT(user_id) DO NOTHING",(uid,))
        conn.commit()
        _cache_invalidate(uid)
    except Exception as e:
        conn.rollback()
        print(f"ensure_user error: {e}")
    finally:
        release(conn)

def get_user(uid: int):
    cached = _cache_get(_user_cache, uid)
    if cached: return cached
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM users WHERE user_id=%s",(uid,))
        row = cur.fetchone()
        if row:
            for k,v in [('credits',0),('prestige',0),('total_coins',0),('daily_streak',0),
                        ('last_msg_ts',0),('last_work_ts',0),('last_rob_ts',0),('work_count',0),('inventory_public',False)]:
                if row.get(k) is None: row[k] = v
            _cache_set(_user_cache, uid, row)
        return row
    except Exception as e:
        print(f"get_user error: {e}"); return None
    finally:
        release(conn)

def get_user_items(uid: int):
    cached = _cache_get(_items_cache, uid)
    if cached: return cached
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM user_items WHERE user_id=%s",(uid,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO user_items(user_id) VALUES(%s) ON CONFLICT DO NOTHING",(uid,))
            conn.commit()
            cur.execute("SELECT * FROM user_items WHERE user_id=%s",(uid,))
            row = cur.fetchone()
        result = dict(row) if row else {}
        _cache_set(_items_cache, uid, result)
        return result
    except Exception as e:
        print(f"get_user_items error: {e}"); return {}
    finally:
        release(conn)

def get_user_bank(uid: int):
    cached = _cache_get(_bank_cache, uid)
    if cached: return cached
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM user_bank WHERE user_id=%s",(uid,))
        row = cur.fetchone()
        if not row:
            bid = generate_bank_id()
            cur.execute("INSERT INTO user_bank(user_id,balance,last_interest,bank_id) VALUES(%s,0,NOW(),%s) ON CONFLICT DO NOTHING",(uid,bid))
            conn.commit()
            return {"balance":0,"last_interest":datetime.now(timezone.utc),"bank_id":bid}
        result = dict(row)
        _cache_set(_bank_cache, uid, result)
        return result
    except Exception as e:
        print(f"get_user_bank error: {e}")
        return {"balance":0,"last_interest":datetime.now(timezone.utc),"bank_id":None}
    finally:
        release(conn)

def add_credits(uid: int, amount: int, reason: str = ""):
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s",(amount,uid))
        if reason:
            cur.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,%s)",(uid,amount,reason))
        conn.commit()
        _cache_invalidate(uid)
    except Exception as e:
        conn.rollback(); print(f"add_credits error: {e}")
    finally:
        release(conn)

def sync_coin_count(uid: int, cur):
    cur.execute("UPDATE users SET total_coins=(SELECT COUNT(*) FROM coins WHERE owner_id=%s) WHERE user_id=%s",(uid,uid))

def get_bank_total():
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT total FROM bank WHERE id=1")
        row = cur.fetchone(); return row['total'] if row else 0
    finally: release(conn)

def count_users():
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) as c FROM users")
        row = cur.fetchone(); return row['c'] if row else 1
    finally: release(conn)

def apply_bank_interest(uid: int) -> int:
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM user_bank WHERE user_id=%s",(uid,))
        row = cur.fetchone()
        if not row or row['balance'] <= 0: return 0
        now  = datetime.now(timezone.utc)
        last = row['last_interest']
        if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
        periods = int((now - last).total_seconds() / 60 // BANK_INTEREST_MINS)
        if periods <= 0: return 0
        new_bal  = int(row['balance'] * (1 + BANK_INTEREST_PCT) ** periods)
        interest = new_bal - row['balance']
        cur.execute("UPDATE user_bank SET balance=%s,last_interest=%s WHERE user_id=%s",(new_bal,now,uid))
        conn.commit()
        _cache_invalidate(uid)
        return interest
    except Exception as e:
        conn.rollback(); print(f"apply_bank_interest error: {e}"); return 0
    finally: release(conn)

def get_coin_rap(coin_id: int):
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT AVG(price) as rap FROM coin_trades WHERE coin_id=%s ORDER BY traded_at DESC LIMIT 10",(coin_id,))
        row = cur.fetchone()
        return round(float(row['rap']),2) if row and row['rap'] else None
    except: return None
    finally: release(conn)

def get_hack_progress(hacker_id: int, target_bank_id: str) -> int:
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT fail_count FROM hack_progress WHERE hacker_id=%s AND target_bank_id=%s",(hacker_id,target_bank_id))
        row = cur.fetchone(); return row['fail_count'] if row else 0
    finally: release(conn)

def increment_hack_fail(hacker_id: int, target_bank_id: str):
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO hack_progress(hacker_id,target_bank_id,fail_count) VALUES(%s,%s,1)
            ON CONFLICT(hacker_id,target_bank_id) DO UPDATE SET fail_count=hack_progress.fail_count+1,updated_at=NOW()""",
            (hacker_id,target_bank_id))
        conn.commit()
    except Exception as e:
        conn.rollback(); print(f"increment_hack_fail error: {e}")
    finally: release(conn)

def log_material_sale_db(material: str):
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO material_sales_log(material,total_sold) VALUES(%s,1) ON CONFLICT(material) DO UPDATE SET total_sold=material_sales_log.total_sold+1",(material,))
        conn.commit()
    except: conn.rollback()
    finally: release(conn)

def get_portfolio_value(uid: int) -> float:
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT COALESCE(SUM(value),0) as pv FROM coins WHERE owner_id=%s",(uid,))
        return float(cur.fetchone()['pv'])
    finally: release(conn)

# ─── Bot Setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='-', intents=intents, help_command=None)

# ─── Message Listener ──────────────────────────────────────────────────────────
@bot.event
async def on_message(message):
    if message.author.bot: return
    uid = message.author.id
    now = time.monotonic()
    ensure_user(uid, str(message.author))
    if now - MSG_COOLDOWNS.get(uid, 0) >= MSG_COOLDOWN_S:
        MSG_COOLDOWNS[uid] = now
        u = get_user(uid)
        prestige_val = (u.get('prestige') or 0) if u else 0
        add_credits(uid, int(CREDITS_PER_MSG * prestige_multiplier(prestige_val)), "message")
    await bot.process_commands(message)

# ─── Background Tasks ──────────────────────────────────────────────────────────
@tasks.loop(minutes=20)
async def update_market_prices():
    event = generate_market_prices()
    if event and _announce_channel_id:
        ch = bot.get_channel(_announce_channel_id)
        if ch:
            ev_type, mat, mult = event
            msg = (f"📈 **MARKET BOOM!** **{mat}** surging **×{mult}** for {EVENT_DURATION_MINS}min! 💰"
                   if ev_type == "boom" else
                   f"📉 **MARKET BUST!** **{mat}** crashed to **×{mult}** for {EVENT_DURATION_MINS}min. 🔻")
            try: await ch.send(msg)
            except: pass

@tasks.loop(minutes=5)
async def auction_checker():
    conn = db(); cur = conn.cursor()
    try:
        now = datetime.now(timezone.utc)
        cur.execute("SELECT * FROM auctions WHERE status='active' AND ends_at<=%s",(now,))
        expired = cur.fetchall()
        for a in expired:
            coin_id = a['coin_id']; seller_id = a['seller_id']
            if a['bidder_id'] and a['current_bid']:
                winner_id = a['bidder_id']; sale_price = a['current_bid']
                fee  = int(round(sale_price * MARKET_FEE_PCT))
                sink = int(round(sale_price * ECONOMY_SINK_PCT))
                net  = sale_price - fee - sink
                cur.execute("UPDATE coins SET owner_id=%s WHERE id=%s",(winner_id,coin_id))
                cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s",(net,seller_id))
                cur.execute("UPDATE bank SET total=total+%s WHERE id=1",(fee,))
                cur.execute("INSERT INTO coin_trades(coin_id,seller_id,buyer_id,price) VALUES(%s,%s,%s,%s)",(coin_id,seller_id,winner_id,sale_price))
                sync_coin_count(winner_id,cur); sync_coin_count(seller_id,cur)
                cur.execute("UPDATE auctions SET status='sold' WHERE id=%s",(a['id'],))
                conn.commit()
                _cache_invalidate(winner_id); _cache_invalidate(seller_id)
                try:
                    w = await bot.fetch_user(winner_id)
                    if w: await w.send(f"🎉 You won auction **#{a['id']}**! **{sale_price:,}** credits spent.")
                except: pass
                try:
                    s = await bot.fetch_user(seller_id)
                    if s: await s.send(f"✅ Auction **#{a['id']}** sold! You received **{net:,}** credits after fees.")
                except: pass
            else:
                cur.execute("UPDATE coins SET owner_id=%s WHERE id=%s",(seller_id,coin_id))
                cur.execute("UPDATE auctions SET status='expired' WHERE id=%s",(a['id'],))
                conn.commit()
                try:
                    s = await bot.fetch_user(seller_id)
                    if s: await s.send(f"📦 Auction **#{a['id']}** expired with no bids. Coin returned.")
                except: pass
    except Exception as e:
        print(f"auction_checker error: {e}")
        try: conn.rollback()
        except: pass
    finally: release(conn)

@tasks.loop(hours=24)
async def daily_bank_distribution():
    total = get_bank_total()
    n     = count_users()
    if total <= 0 or n == 0: return
    share = total // n
    if share <= 0: return
    conn = db(); cur = conn.cursor()
    try:
        today = datetime.now(timezone.utc).date()
        cur.execute("SELECT paid_date FROM daily_log WHERE paid_date=%s",(today,))
        if cur.fetchone(): return
        cur.execute("UPDATE users SET credits=credits+%s",(share,))
        cur.execute("UPDATE bank SET total=0 WHERE id=1")
        cur.execute("INSERT INTO daily_log(paid_date,amount) VALUES(%s,%s)",(today,share))
        conn.commit()
        print(f"✅ Daily payout: {share:,} cr to {n} users.")
    except Exception as e:
        conn.rollback(); print(f"daily_bank_distribution error: {e}")
    finally: release(conn)

@tasks.loop(hours=2)
async def black_market_spawner():
    eligible = set()
    for guild in bot.guilds:
        role = guild.get_role(BLACK_MARKET_ROLE_ID)
        if role:
            for member in role.members:
                eligible.add(member.id)
    for uid in eligible:
        if get_user_bm(uid): continue
        if random.random() < BLACK_MARKET_CHANCE:
            bm = spawn_user_bm(uid)
            try:
                user = await bot.fetch_user(uid)
                e = discord.Embed(title="🌑 YOUR BLACK MARKET APPEARED!",
                    description=f"A shadowy marketplace **just for you** for **{BLACK_MARKET_DURATION_MIN} minutes**!\nUse `-blackmarket` in DMs.",
                    color=0x2F3136)
                e.set_footer(text=f"Expires: {bm['expires'].strftime('%H:%M UTC')}")
                await user.send(embed=e)
            except: pass

@tasks.loop(minutes=5)
async def auto_worker_task():
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM user_items WHERE cognitive_enabled=TRUE OR ai_enabled=TRUE")
        rows = cur.fetchall()
    except Exception as e:
        print(f"auto_worker_task error: {e}"); release(conn); return
    finally: release(conn)

    now_ts = int(time.time())
    for item_row in rows:
        uid = item_row['user_id']
        u   = get_user(uid)
        if not u: continue
        work_count = u.get('work_count') or 0
        prestige_v = u.get('prestige') or 0
        rank_name, rank_salary, _, rank_idx = get_job_rank(work_count)
        prev_min = JOB_RANKS[rank_idx][1]
        within_m = min(1.0 + (work_count - prev_min) * 0.02, 1.5)
        base_pay = min(int(rank_salary * within_m), 5000)

        for machine, cd, mult, col_ts, col_en in [
            ("cognitive", 1800, 0.75, "last_cognitive_ts", "cognitive_enabled"),
            ("ai", 900, 2.0, "last_ai_ts", "ai_enabled"),
        ]:
            if not item_row.get(f"{'has_cognitive_machine' if machine=='cognitive' else 'has_ai_machine'}"): continue
            if not item_row.get(col_en): continue
            if now_ts - (item_row.get(col_ts) or 0) < cd: continue
            earned = int(base_pay * prestige_multiplier(prestige_v) * mult)
            conn2 = db(); cur2 = conn2.cursor()
            try:
                cur2.execute("UPDATE users SET credits=credits+%s,work_count=work_count+1 WHERE user_id=%s",(earned,uid))
                cur2.execute(f"UPDATE user_items SET {col_ts}=%s WHERE user_id=%s",(now_ts,uid))
                cur2.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,%s)",(uid,earned,f"{machine}_auto_work"))
                conn2.commit()
                _cache_invalidate(uid)
            except Exception as e:
                conn2.rollback(); print(f"auto_work {machine} error uid={uid}: {e}")
            finally: release(conn2)

@tasks.loop(minutes=1)
async def hack_transfer_checker():
    conn = db(); cur = conn.cursor()
    try:
        now = datetime.now(timezone.utc)
        cur.execute("SELECT * FROM hack_transfers WHERE done=FALSE AND completes_at<=%s",(now,))
        rows = cur.fetchall()
        for r in rows:
            cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s",(r['amount'],r['hacker_id']))
            cur.execute("UPDATE hack_transfers SET done=TRUE WHERE id=%s",(r['id'],))
            cur.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,'hack_transfer')",(r['hacker_id'],r['amount']))
            conn.commit()
            _cache_invalidate(r['hacker_id'])
            try:
                user = await bot.fetch_user(r['hacker_id'])
                if user: await user.send(f"📡 **Transfer Complete!** **{r['amount']:,} credits** deposited to your wallet.")
            except: pass
    except Exception as e:
        print(f"hack_transfer_checker error: {e}")
        try: conn.rollback()
        except: pass
    finally: release(conn)

# ═══════════════════════════════════════════════════════════════════════════════
#  VIEWS & UI COMPONENTS
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Quick Action Buttons (reusable footer for many embeds) ───────────────────
class QuickNav(discord.ui.View):
    """Universal quick-navigation buttons shown on most command responses."""
    def __init__(self, uid: int):
        super().__init__(timeout=60)
        self.uid = uid

    @discord.ui.button(label="💳 Balance", style=discord.ButtonStyle.grey)
    async def btn_balance(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("Not your panel.", ephemeral=True); return
        await interaction.response.defer()
        ctx_like = interaction
        await _send_balance(interaction, self.uid)

    @discord.ui.button(label="🎒 Inventory", style=discord.ButtonStyle.grey)
    async def btn_inv(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("Not your panel.", ephemeral=True); return
        await interaction.response.defer()
        await _send_inventory(interaction, self.uid, page=1)

    @discord.ui.button(label="⏱️ Cooldowns", style=discord.ButtonStyle.grey)
    async def btn_cd(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("Not your panel.", ephemeral=True); return
        await interaction.response.defer()
        await _send_cooldowns(interaction, self.uid)

    @discord.ui.button(label="🛒 Shop", style=discord.ButtonStyle.blurple)
    async def btn_shop(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("Not your panel.", ephemeral=True); return
        await interaction.response.defer()
        await _send_shop(interaction, self.uid)

# ─── Inventory View with filters & pagination ─────────────────────────────────
class InventoryView(discord.ui.View):
    def __init__(self, uid: int, target_uid: int, page: int=1,
                 sort="value_desc", filter_mat=None, filter_status=None, filter_float=None):
        super().__init__(timeout=120)
        self.uid         = uid
        self.target_uid  = target_uid
        self.page        = page
        self.sort        = sort
        self.filter_mat  = filter_mat
        self.filter_status = filter_status
        self.filter_float  = filter_float
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page <= 1
        sort_labels = {"value_desc":"📦 Value↓","value_asc":"📦 Value↑","market_desc":"📈 Market↓","rarity_desc":"✨ Rarity↓","newest":"🆕 Newest"}
        self.sort_btn.label = sort_labels.get(self.sort, "Sort")

    def _build_query(self):
        conditions = ["owner_id=%s"]
        params     = [self.target_uid]
        if self.filter_mat:
            conditions.append("material=%s"); params.append(self.filter_mat)
        if self.filter_status:
            conditions.append("status=%s"); params.append(self.filter_status)
        if self.filter_float:
            conditions.append("float=%s"); params.append(self.filter_float)
        where = " AND ".join(conditions)
        order = {"value_desc":"value DESC","value_asc":"value ASC",
                 "market_desc":"value DESC","rarity_desc":"(mat_mult+var_mult+sta_mult+flt_mult+ser_mult) DESC",
                 "newest":"obtained_at DESC"}.get(self.sort,"value DESC")
        return where, order, params

    async def _refresh(self, interaction: discord.Interaction):
        await _send_inventory_view(interaction, self.uid, self.target_uid, self.page,
                                   self.sort, self.filter_mat, self.filter_status, self.filter_float)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.grey)
    async def prev_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: await interaction.response.send_message("Not yours.", ephemeral=True); return
        self.page -= 1; await self._refresh(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.grey)
    async def next_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: await interaction.response.send_message("Not yours.", ephemeral=True); return
        self.page += 1; await self._refresh(interaction)

    @discord.ui.button(label="📦 Value↓", style=discord.ButtonStyle.blurple)
    async def sort_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: await interaction.response.send_message("Not yours.", ephemeral=True); return
        cycle = ["value_desc","value_asc","market_desc","rarity_desc","newest"]
        idx   = cycle.index(self.sort) if self.sort in cycle else 0
        self.sort = cycle[(idx+1) % len(cycle)]
        self.page = 1; await self._refresh(interaction)

    @discord.ui.button(label="🔍 Filter", style=discord.ButtonStyle.green)
    async def filter_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: await interaction.response.send_message("Not yours.", ephemeral=True); return
        await interaction.response.send_modal(InventoryFilterModal(self))

    @discord.ui.button(label="🗑️ Clear Filter", style=discord.ButtonStyle.red)
    async def clear_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: await interaction.response.send_message("Not yours.", ephemeral=True); return
        self.filter_mat = self.filter_status = self.filter_float = None
        self.page = 1; await self._refresh(interaction)

class InventoryFilterModal(discord.ui.Modal, title="🔍 Filter Inventory"):
    material = discord.ui.TextInput(label="Material (blank = all)", required=False, placeholder="Gold, Diamond, Plasma...")
    status   = discord.ui.TextInput(label="Status (blank = all)", required=False, placeholder="Shiny, Stunning, Normal...")
    float_v  = discord.ui.TextInput(label="Float (blank = all)", required=False, placeholder="Godlike, Illusional...")

    def __init__(self, view: InventoryView):
        super().__init__()
        self.inv_view = view

    async def on_submit(self, interaction: discord.Interaction):
        self.inv_view.filter_mat    = self.material.value.strip() or None
        self.inv_view.filter_status = self.status.value.strip() or None
        self.inv_view.filter_float  = self.float_v.value.strip() or None
        self.inv_view.page = 1
        await self.inv_view._refresh(interaction)

async def _send_inventory_view(interaction, uid, target_uid, page, sort, filter_mat, filter_status, filter_float):
    PER_PAGE = 8
    conditions = ["owner_id=%s"]; params = [target_uid]
    if filter_mat:    conditions.append("material=%s"); params.append(filter_mat)
    if filter_status: conditions.append("status=%s");   params.append(filter_status)
    if filter_float:  conditions.append("float=%s");    params.append(filter_float)
    where  = " AND ".join(conditions)
    order  = {"value_desc":"value DESC","value_asc":"value ASC",
              "market_desc":"value DESC","rarity_desc":"(mat_mult+var_mult+sta_mult+flt_mult+ser_mult) DESC",
              "newest":"obtained_at DESC"}.get(sort,"value DESC")
    offset = (page-1) * PER_PAGE

    conn = db(); cur = conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) as c FROM coins WHERE {where}", params)
        total = cur.fetchone()['c']
        cur.execute(f"SELECT * FROM coins WHERE {where} ORDER BY {order} LIMIT %s OFFSET %s", params+[PER_PAGE,offset])
        coins = cur.fetchall()
    finally: release(conn)

    pages = max(1, math.ceil(total/PER_PAGE))
    if page > pages and pages > 0: page = pages

    view = InventoryView(uid, target_uid, page, sort, filter_mat, filter_status, filter_float)
    view.prev_btn.disabled = page <= 1
    view.next_btn.disabled = page >= pages

    u = get_user(target_uid)
    uname = u['username'] if u else str(target_uid)

    filters_str = ""
    if filter_mat:    filters_str += f" | Material: **{filter_mat}**"
    if filter_status: filters_str += f" | Status: **{filter_status}**"
    if filter_float:  filters_str += f" | Float: **{filter_float}**"

    sort_labels = {"value_desc":"Value ↓","value_asc":"Value ↑","market_desc":"Market ↓","rarity_desc":"Rarity ↓","newest":"Newest First"}
    e = discord.Embed(title=f"🎒 {uname}'s Inventory", color=0x5865F2)
    e.description = f"Page **{page}/{pages}** | **{total}** coins | Sort: **{sort_labels.get(sort,'?')}**{filters_str}\n\n"

    if not coins:
        e.description += "No coins found with current filters."
    else:
        lines = []
        for c in coins:
            mkt   = get_market_price(c)
            tier  = tier_emoji(mkt)
            name  = coin_name(c)
            rap   = get_coin_rap(c['id'])
            ev    = get_event_mult(c['material'])
            ev_tag = " 🚀" if ev > 1.0 else (" 💥" if ev < 1.0 else "")
            rap_str = f" | RAP:{rap:,.0f}" if rap else ""
            lines.append(
                f"{tier} `#{c['id']}` **{name}** `#{str(c['serial']).zfill(4)}`\n"
                f"  {c['status']} · {c['float']} · 📈 **${mkt:.4f}**{ev_tag}{rap_str}"
            )
        e.description += "\n\n".join(lines)

    e.set_footer(text="Use -coin <id> for details • -sell <id> to sell")
    try:
        if hasattr(interaction, 'response') and not interaction.response.is_done():
            await interaction.response.edit_message(embed=e, view=view)
        else:
            await interaction.edit_original_response(embed=e, view=view)
    except:
        try: await interaction.followup.send(embed=e, view=view)
        except: pass

# ─── Leaderboard View ─────────────────────────────────────────────────────────
class LeaderboardView(discord.ui.View):
    def __init__(self, uid: int, mode: str="portfolio"):
        super().__init__(timeout=90)
        self.uid  = uid
        self.mode = mode
        self._update()

    def _update(self):
        modes = {"portfolio":"📈 Portfolio","credits":"💰 Credits","coins":"🪙 Coins","jobs":"💼 Jobs","bank":"🏦 Bank"}
        self.mode_btn.label = modes.get(self.mode, "Leaderboard")

    @discord.ui.button(label="📈 Portfolio", style=discord.ButtonStyle.blurple)
    async def mode_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        cycle = ["portfolio","credits","coins","jobs","bank"]
        idx   = cycle.index(self.mode) if self.mode in cycle else 0
        self.mode = cycle[(idx+1) % len(cycle)]
        self._update()
        await _send_leaderboard_view(interaction, self.uid, self.mode)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.grey)
    async def refresh_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await _send_leaderboard_view(interaction, self.uid, self.mode)

async def _send_leaderboard_view(interaction, uid, mode):
    conn = db(); cur = conn.cursor()
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    try:
        if mode == "portfolio":
            # Use market prices for LB — calculate per-user market value
            cur.execute("""
                SELECT u.username, u.prestige, u.work_count, u.total_coins,
                       COALESCE(SUM(c.base_value * c.mat_mult * c.var_mult * c.sta_mult * c.flt_mult * c.ser_mult), 0) as portfolio
                FROM users u LEFT JOIN coins c ON c.owner_id=u.user_id
                GROUP BY u.user_id, u.username, u.prestige, u.work_count, u.total_coins
                ORDER BY portfolio DESC LIMIT 10
            """)
            rows = cur.fetchall()
            title = "🏆 Top Collectors — Market Portfolio Value"
            color = 0xFFD700
            def row_val(r): return f"Market Portfolio: **${float(r['portfolio']):.2f}** | Coins: **{r['total_coins']}**"
        elif mode == "credits":
            cur.execute("SELECT username,credits,prestige,work_count FROM users ORDER BY credits DESC LIMIT 10")
            rows = cur.fetchall()
            title = "💰 Credit Rich List"
            color = 0x57F287
            def row_val(r): return f"**{r['credits']:,} credits**"
        elif mode == "coins":
            cur.execute("SELECT username,total_coins,prestige,work_count FROM users ORDER BY total_coins DESC LIMIT 10")
            rows = cur.fetchall()
            title = "🪙 Most Coins"
            color = 0x5865F2
            def row_val(r): return f"**{r['total_coins']}** coins"
        elif mode == "jobs":
            cur.execute("SELECT username,work_count,prestige FROM users ORDER BY work_count DESC LIMIT 10")
            rows = cur.fetchall()
            title = "💼 Hardest Workers"
            color = 0xEB459E
            def row_val(r): return f"**{r.get('work_count',0):,}** jobs worked"
        elif mode == "bank":
            cur.execute("""
                SELECT u.username, u.prestige, u.work_count, ub.balance
                FROM users u JOIN user_bank ub ON ub.user_id=u.user_id
                ORDER BY ub.balance DESC LIMIT 10
            """)
            rows = cur.fetchall()
            title = "🏦 Bank Balances"
            color = 0x57F287
            def row_val(r): return f"Bank: **{r['balance']:,}** credits"
        else:
            rows = []; title = "Leaderboard"; color = 0xFFD700
            def row_val(r): return ""
    except Exception as e:
        print(f"LB error: {e}"); conn.rollback()
        rows = []; title = "Leaderboard"; color = 0xFFD700
        def row_val(r): return ""
    finally: release(conn)

    e = discord.Embed(title=title, color=color)
    for i,r in enumerate(rows):
        prestige_v = r.get('prestige') or 0
        work_count = r.get('work_count') or 0
        rank_name, _, rank_emoji, _ = get_job_rank(work_count)
        star = f" ⭐×{prestige_v}" if prestige_v else ""
        e.add_field(name=f"{medals[i]} {r['username']}{star} — {rank_emoji} {rank_name}",
                    value=row_val(r), inline=False)
    if not rows: e.description = "No data yet."

    view = LeaderboardView(uid, mode)
    try:
        if hasattr(interaction,'response') and not interaction.response.is_done():
            await interaction.response.edit_message(embed=e, view=view)
        else:
            await interaction.edit_original_response(embed=e, view=view)
    except:
        try: await interaction.followup.send(embed=e, view=view)
        except: pass

# ─── Balance View ──────────────────────────────────────────────────────────────
class BalanceView(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=60)
        self.uid = uid

    @discord.ui.button(label="💼 Work", style=discord.ButtonStyle.green)
    async def work_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: await interaction.response.send_message("Not yours.", ephemeral=True); return
        ctx = await bot.get_context(interaction.message)
        ctx.author = interaction.user
        await interaction.response.defer()
        await _do_work(interaction, self.uid)

    @discord.ui.button(label="📅 Daily", style=discord.ButtonStyle.green)
    async def daily_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: await interaction.response.send_message("Not yours.", ephemeral=True); return
        await interaction.response.defer()
        await _do_daily(interaction, self.uid)

    @discord.ui.button(label="🏦 Bank", style=discord.ButtonStyle.blurple)
    async def bank_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: await interaction.response.send_message("Not yours.", ephemeral=True); return
        await interaction.response.defer()
        await _send_vbank(interaction, self.uid)

    @discord.ui.button(label="📦 Open Crate", style=discord.ButtonStyle.blurple)
    async def crate_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: await interaction.response.send_message("Not yours.", ephemeral=True); return
        await interaction.response.defer()
        await _do_buy_crate(interaction, self.uid, 1)

    @discord.ui.button(label="⏱️ Cooldowns", style=discord.ButtonStyle.grey)
    async def cd_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: await interaction.response.send_message("Not yours.", ephemeral=True); return
        await interaction.response.defer()
        await _send_cooldowns(interaction, self.uid)

# ─── Shop View ─────────────────────────────────────────────────────────────────
class ShopView(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=90)
        self.uid = uid

    @discord.ui.button(label="📦 1 Crate", style=discord.ButtonStyle.green, row=0)
    async def crate1(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.uid: await i.response.send_message("Not yours.",ephemeral=True); return
        await i.response.defer()
        await _do_buy_crate(i, self.uid, 1)

    @discord.ui.button(label="📦×3 Crates", style=discord.ButtonStyle.green, row=0)
    async def crate3(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.uid: await i.response.send_message("Not yours.",ephemeral=True); return
        await i.response.defer()
        await _do_buy_crate(i, self.uid, 3)

    @discord.ui.button(label="📦×5 Crates", style=discord.ButtonStyle.green, row=0)
    async def crate5(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.uid: await i.response.send_message("Not yours.",ephemeral=True); return
        await i.response.defer()
        await _do_buy_crate(i, self.uid, 5)

    @discord.ui.button(label="⚡ Market Trigger", style=discord.ButtonStyle.blurple, row=1)
    async def mktrigger(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.uid: await i.response.send_message("Not yours.",ephemeral=True); return
        await i.response.defer()
        await _do_market_trigger(i, self.uid)

    @discord.ui.button(label="💳 Balance", style=discord.ButtonStyle.grey, row=1)
    async def bal_btn(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.uid: await i.response.send_message("Not yours.",ephemeral=True); return
        await i.response.defer()
        await _send_balance(i, self.uid)

# ─── Sell Confirm View ─────────────────────────────────────────────────────────
class SellConfirmView(discord.ui.View):
    def __init__(self, uid: int, coin_id: int, market_val: float, coin_row):
        super().__init__(timeout=30)
        self.uid = uid; self.coin_id = coin_id
        self.market_val = market_val; self.coin_row = coin_row

    @discord.ui.button(label="✅ Confirm Sell", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: await interaction.response.send_message("Not yours.", ephemeral=True); return
        await interaction.response.defer()
        await _do_sell_coin(interaction, self.uid, self.coin_id)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: await interaction.response.send_message("Not yours.", ephemeral=True); return
        await interaction.response.edit_message(content="❌ Sale cancelled.", embed=None, view=None)
        self.stop()

# ─── SellAll Confirm ───────────────────────────────────────────────────────────
class SellAllConfirmView(discord.ui.View):
    def __init__(self, uid: int, count: int, total_cr: int):
        super().__init__(timeout=30)
        self.uid = uid; self.count = count; self.total_cr = total_cr

    @discord.ui.button(label="✅ Sell All", style=discord.ButtonStyle.red)
    async def confirm(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: await interaction.response.send_message("Not yours.", ephemeral=True); return
        await interaction.response.defer()
        await _do_sellall(interaction, self.uid)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: await interaction.response.send_message("Not yours.", ephemeral=True); return
        await interaction.response.edit_message(content="❌ Cancelled.", embed=None, view=None)
        self.stop()

# ─── Deposit/Withdraw Modal ────────────────────────────────────────────────────
class DepositModal(discord.ui.Modal, title="🏦 Deposit Credits"):
    amount_input = discord.ui.TextInput(label="Amount (or 'all')", placeholder="e.g. 500 or all")
    def __init__(self, uid): super().__init__(); self.uid = uid
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await _do_deposit(interaction, self.uid, self.amount_input.value.strip())

class WithdrawModal(discord.ui.Modal, title="🏦 Withdraw Credits"):
    amount_input = discord.ui.TextInput(label="Amount (or 'all')", placeholder="e.g. 500 or all")
    def __init__(self, uid): super().__init__(); self.uid = uid
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await _do_withdraw(interaction, self.uid, self.amount_input.value.strip())

# ─── VBank View ────────────────────────────────────────────────────────────────
class VBankView(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=60)
        self.uid = uid

    @discord.ui.button(label="⬆️ Deposit", style=discord.ButtonStyle.green)
    async def dep(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.uid: await i.response.send_message("Not yours.",ephemeral=True); return
        await i.response.send_modal(DepositModal(self.uid))

    @discord.ui.button(label="⬇️ Withdraw", style=discord.ButtonStyle.blurple)
    async def wit(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.uid: await i.response.send_message("Not yours.",ephemeral=True); return
        await i.response.send_modal(WithdrawModal(self.uid))

    @discord.ui.button(label="🔑 My Bank ID", style=discord.ButtonStyle.grey)
    async def bid(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.uid: await i.response.send_message("Not yours.",ephemeral=True); return
        vb = get_user_bank(self.uid)
        bid = vb.get('bank_id') or "Not generated yet."
        await i.response.send_message(f"🏦 **Your Bank ID:**\n```\n{bid}\n```\n⚠️ Keep this private!", ephemeral=True)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.grey)
    async def ref(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.uid: await i.response.send_message("Not yours.",ephemeral=True); return
        await i.response.defer()
        await _send_vbank(i, self.uid)

# ─── Gamble Views ──────────────────────────────────────────────────────────────
class CoinflipView(discord.ui.View):
    def __init__(self, uid: int, bet: int):
        super().__init__(timeout=30); self.uid = uid; self.bet = bet

    @discord.ui.button(label="🪙 Heads", style=discord.ButtonStyle.blurple)
    async def heads(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.uid: await i.response.send_message("Not your game.",ephemeral=True); return
        await self.resolve(i,"heads")

    @discord.ui.button(label="🪙 Tails", style=discord.ButtonStyle.grey)
    async def tails(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.uid: await i.response.send_message("Not your game.",ephemeral=True); return
        await self.resolve(i,"tails")

    async def resolve(self, i: discord.Interaction, choice: str):
        result = random.choice(["heads","tails"]); won = choice==result
        conn = db(); cur = conn.cursor()
        try:
            if won:
                cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s",(self.bet,self.uid))
                cur.execute("UPDATE bank SET total=total+%s WHERE id=1",(max(1,int(self.bet*0.02)),))
                color,title,desc = discord.Color.green(),"🎉 You Won!",f"Landed **{result}**! Won **{self.bet:,} credits**."
            else:
                cur.execute("UPDATE users SET credits=GREATEST(0,credits-%s) WHERE user_id=%s",(self.bet,self.uid))
                cur.execute("UPDATE bank SET total=total+%s WHERE id=1",(max(1,int(self.bet*0.50)),))
                color,title,desc = discord.Color.red(),"💸 You Lost!",f"Landed **{result}**. Lost **{self.bet:,} credits**."
            conn.commit(); _cache_invalidate(self.uid)
        except Exception as e:
            conn.rollback(); color,title,desc = discord.Color.red(),"Error","Something went wrong."
        finally: release(conn)
        await i.response.edit_message(embed=discord.Embed(title=title,description=desc,color=color),view=None)
        self.stop()

# ─── Trade View ────────────────────────────────────────────────────────────────
class TradeView(discord.ui.View):
    def __init__(self, trade_id, initiator_id, receiver_id):
        super().__init__(timeout=120)
        self.trade_id=trade_id; self.initiator_id=initiator_id; self.receiver_id=receiver_id

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.green)
    async def accept(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.receiver_id: await i.response.send_message("Only the recipient can accept.",ephemeral=True); return
        await self.resolve(i,True)

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.red)
    async def decline(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id not in (self.receiver_id,self.initiator_id): await i.response.send_message("Not your trade.",ephemeral=True); return
        await self.resolve(i,False)

    async def resolve(self, i, accepted: bool):
        conn = db(); cur = conn.cursor()
        try:
            cur.execute("SELECT * FROM trades WHERE id=%s AND status='pending'",(self.trade_id,))
            trade = cur.fetchone()
            if not trade: await i.response.send_message("⚠️ Trade no longer active.",ephemeral=True); return
            if not accepted:
                cur.execute("UPDATE trades SET status='declined' WHERE id=%s",(self.trade_id,))
                conn.commit()
                await i.response.edit_message(content="❌ Trade declined.",embed=None,view=None); self.stop(); return
            coin_ids = [int(x) for x in trade['coin_ids'].split(',') if x.strip()] if trade['coin_ids'] else []
            coff = trade['credits_offer']
            if coin_ids:
                phs = ','.join(['%s']*len(coin_ids))
                cur.execute(f"SELECT id,owner_id FROM coins WHERE id IN ({phs})",coin_ids)
                for r in cur.fetchall():
                    if r['owner_id']!=trade['initiator_id']:
                        cur.execute("UPDATE trades SET status='invalid' WHERE id=%s",(self.trade_id,)); conn.commit()
                        await i.response.edit_message(content="❌ Coin ownership changed; cancelled.",embed=None,view=None); self.stop(); return
            if coff>0:
                cur.execute("SELECT credits FROM users WHERE user_id=%s",(trade['initiator_id'],))
                row = cur.fetchone()
                if not row or row['credits']<coff:
                    cur.execute("UPDATE trades SET status='invalid' WHERE id=%s",(self.trade_id,)); conn.commit()
                    await i.response.edit_message(content="❌ Initiator has insufficient credits.",embed=None,view=None); self.stop(); return
            if coin_ids:
                phs = ','.join(['%s']*len(coin_ids))
                cur.execute(f"UPDATE coins SET owner_id=%s WHERE id IN ({phs})",[trade['receiver_id']]+coin_ids)
                per = coff//len(coin_ids) if coin_ids else 0
                for cid in coin_ids:
                    cur.execute("INSERT INTO coin_trades(coin_id,seller_id,buyer_id,price) VALUES(%s,%s,%s,%s)",(cid,trade['initiator_id'],trade['receiver_id'],per))
            if coff>0:
                tax=int(round(coff*TRADE_TAX_PCT)); sink=int(round(coff*ECONOMY_SINK_PCT)); net=coff-tax-sink
                cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s",(coff,trade['initiator_id']))
                cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s",(net,trade['receiver_id']))
                cur.execute("UPDATE bank SET total=total+%s WHERE id=1",(tax,))
            for u2 in (trade['initiator_id'],trade['receiver_id']):
                sync_coin_count(u2,cur); _cache_invalidate(u2)
            cur.execute("UPDATE trades SET status='completed' WHERE id=%s",(self.trade_id,))
            conn.commit()
            await i.response.edit_message(embed=discord.Embed(title="✅ Trade Completed!",color=discord.Color.green()),view=None)
            self.stop()
        except Exception as e:
            conn.rollback(); print(f"resolve_trade error: {e}")
            try: await i.response.send_message("❌ Error processing trade.",ephemeral=True)
            except: pass
        finally: release(conn)

# ─── Auction Views ─────────────────────────────────────────────────────────────
class BidModal(discord.ui.Modal, title="Place a Bid"):
    amount = discord.ui.TextInput(label="Bid Amount (credits)", placeholder="e.g. 500")
    def __init__(self, auction_id): super().__init__(); self.auction_id=auction_id

    async def on_submit(self, i: discord.Interaction):
        try: bid=int(self.amount.value)
        except: await i.response.send_message("❌ Enter a whole number.",ephemeral=True); return
        uid = i.user.id; ensure_user(uid,str(i.user))
        conn=db(); cur=conn.cursor()
        try:
            cur.execute("SELECT * FROM auctions WHERE id=%s AND status='active'",(self.auction_id,))
            a=cur.fetchone()
            if not a: await i.response.send_message("❌ Auction not found or ended.",ephemeral=True); return
            if uid==a['seller_id']: await i.response.send_message("❌ Can't bid on your own auction.",ephemeral=True); return
            min_bid=max(a['start_price'],(a['current_bid'] or 0)+1)
            if bid<min_bid: await i.response.send_message(f"❌ Min bid: **{min_bid:,}**.",ephemeral=True); return
            cur.execute("SELECT credits FROM users WHERE user_id=%s",(uid,))
            u=cur.fetchone()
            if not u or u['credits']<bid: await i.response.send_message(f"❌ You have **{u['credits']:,}** credits.",ephemeral=True); return
            if a['bidder_id'] and a['current_bid']: cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s",(a['current_bid'],a['bidder_id']))
            cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s",(bid,uid))
            cur.execute("UPDATE auctions SET current_bid=%s,bidder_id=%s WHERE id=%s",(bid,uid,self.auction_id))
            conn.commit(); _cache_invalidate(uid)
            await i.response.send_message(f"✅ Bid of **{bid:,}** placed on auction **#{self.auction_id}**!",ephemeral=True)
        except Exception as e:
            conn.rollback(); print(f"BidModal error: {e}")
            try: await i.response.send_message("❌ Error placing bid.",ephemeral=True)
            except: pass
        finally: release(conn)

class AuctionView(discord.ui.View):
    def __init__(self, auction_id): super().__init__(timeout=None); self.auction_id=auction_id
    @discord.ui.button(label="💰 Place Bid", style=discord.ButtonStyle.blurple)
    async def bid_btn(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(BidModal(self.auction_id))

class MarketView(discord.ui.View):
    def __init__(self, uid: int, page: int=1, sort: str="ends_asc", filter_mat=None):
        super().__init__(timeout=90)
        self.uid=uid; self.page=page; self.sort=sort; self.filter_mat=filter_mat

    @discord.ui.button(label="◀", style=discord.ButtonStyle.grey)
    async def prev(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.uid: await i.response.send_message("Not yours.",ephemeral=True); return
        self.page-=1; await _send_market_view(i, self.uid, self.page, self.sort, self.filter_mat)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.grey)
    async def next_p(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.uid: await i.response.send_message("Not yours.",ephemeral=True); return
        self.page+=1; await _send_market_view(i, self.uid, self.page, self.sort, self.filter_mat)

    @discord.ui.button(label="⏰ Ending Soon", style=discord.ButtonStyle.blurple)
    async def sort_btn(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.uid: await i.response.send_message("Not yours.",ephemeral=True); return
        cycle=["ends_asc","price_asc","price_desc","market_desc"]
        idx=cycle.index(self.sort) if self.sort in cycle else 0
        self.sort=cycle[(idx+1)%len(cycle)]; self.page=1
        await _send_market_view(i, self.uid, self.page, self.sort, self.filter_mat)

    @discord.ui.button(label="🔍 Filter Material", style=discord.ButtonStyle.green)
    async def filter_btn(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.uid: await i.response.send_message("Not yours.",ephemeral=True); return
        await i.response.send_modal(MarketFilterModal(self))

    @discord.ui.button(label="🗑️ Clear", style=discord.ButtonStyle.red)
    async def clear_btn(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.uid: await i.response.send_message("Not yours.",ephemeral=True); return
        self.filter_mat=None; self.page=1
        await _send_market_view(i, self.uid, self.page, self.sort, self.filter_mat)

class MarketFilterModal(discord.ui.Modal, title="Filter Marketplace"):
    material = discord.ui.TextInput(label="Material (blank = all)", required=False, placeholder="Gold, Diamond...")
    def __init__(self, view: MarketView): super().__init__(); self.mv=view
    async def on_submit(self, i: discord.Interaction):
        self.mv.filter_mat = self.material.value.strip() or None
        self.mv.page=1
        await _send_market_view(i, self.mv.uid, self.mv.page, self.mv.sort, self.mv.filter_mat)

async def _send_market_view(interaction, uid, page, sort, filter_mat):
    PER_PAGE=5
    conditions=["a.status='active'"]; params=[]
    if filter_mat: conditions.append("c.material=%s"); params.append(filter_mat)
    where=" AND ".join(conditions)
    order={"ends_asc":"a.ends_at ASC","price_asc":"a.start_price ASC",
           "price_desc":"a.current_bid DESC NULLS LAST","market_desc":"c.value DESC"}.get(sort,"a.ends_at ASC")
    offset=(page-1)*PER_PAGE

    conn=db(); cur=conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) as c FROM auctions a JOIN coins c ON c.id=a.coin_id WHERE {where}",params)
        total=cur.fetchone()['c'] or 0
        cur.execute(f"""SELECT a.*,c.material,c.variant,c.status as cond,c.float,c.serial,
                        c.base_value,c.mat_mult,c.var_mult,c.sta_mult,c.flt_mult,c.ser_mult,
                        c.total_mult,c.value as coin_val,c.custom_name,c.obtained_at,u.username as seller_name
                    FROM auctions a JOIN coins c ON c.id=a.coin_id JOIN users u ON u.user_id=a.seller_id
                    WHERE {where} ORDER BY {order} LIMIT %s OFFSET %s""",params+[PER_PAGE,offset])
        rows=cur.fetchall()
    finally: release(conn)

    pages=max(1,math.ceil(total/PER_PAGE))
    sort_labels={"ends_asc":"⏰ Ending Soon","price_asc":"💰 Price ↑","price_desc":"💰 Price ↓","market_desc":"📈 Market ↓"}

    e=discord.Embed(title=f"🏪 Marketplace — Page {page}/{pages}",color=0xEB459E)
    flt_str=f" | Filter: **{filter_mat}**" if filter_mat else ""
    e.description=f"**{total}** listings | Sort: **{sort_labels.get(sort,'?')}**{flt_str}\n"
    for a in rows:
        mkt=get_market_price(a); tier=tier_emoji(mkt)
        top_bid=f"{a['current_bid']:,}" if a['current_bid'] else "No bids"
        name=a.get('custom_name') or f"{a['variant']} {a['material']} Coin"
        ends_ts=a['ends_at']
        if ends_ts.tzinfo is None: ends_ts=ends_ts.replace(tzinfo=timezone.utc)
        ev=get_event_mult(a['material']); ev_tag=" 🚀" if ev>1.0 else (" 💥" if ev<1.0 else "")
        e.add_field(name=f"{tier} #{a['id']} — {name}{ev_tag}",
            value=(f"Cond: **{a['cond']}** · Float: **{a['float']}** · Mkt: **${mkt:.4f}**\n"
                   f"Start: **{a['start_price']:,}** · Top Bid: **{top_bid}** · Seller: {a['seller_name']}\n"
                   f"Ends: <t:{int(ends_ts.timestamp())}:R>"),inline=False)

    view=MarketView(uid,page,sort,filter_mat)
    view.prev.disabled=page<=1; view.next_p.disabled=page>=pages
    try:
        if hasattr(interaction,'response') and not interaction.response.is_done():
            await interaction.response.edit_message(embed=e,view=view)
        else:
            await interaction.edit_original_response(embed=e,view=view)
    except:
        try: await interaction.followup.send(embed=e,view=view)
        except: pass

# ─── Hacking Views ─────────────────────────────────────────────────────────────
class HackChallengeView:
    def __init__(self, hacker_id, target_bank_id, target_uid, hack_chance):
        self.hacker_id=hacker_id; self.target_bank_id=target_bank_id
        self.target_uid=target_uid; self.hack_chance=hack_chance
        self.stage=0; self.passed=0; self.required=3
        self._gen()
    def _gen(self):
        t=random.choice(["math","sequence","password"]); self.current_type=t
        if t=="math":
            a,b=random.randint(10,99),random.randint(10,99); op=random.choice(['+','-','*'])
            self.challenge=f"Solve: `{a} {op} {b}`"; self.answer=str(eval(f"{a}{op}{b}"))
        elif t=="sequence":
            seq=[random.randint(1,9) for _ in range(5)]; hi=random.randint(1,3)
            disp=[str(n) if i!=hi else "?" for i,n in enumerate(seq)]
            self.challenge=f"Missing number: `{' '.join(disp)}`"; self.answer=str(seq[hi])
        else:
            chars="ABCDE12345"; pw=''.join(random.choices(chars,k=4))
            sc=list(pw); random.shuffle(sc)
            self.challenge=f"Unscramble: `{''.join(sc)}`"; self.answer=pw

class HackInputModal(discord.ui.Modal, title="🔓 Hacking Terminal"):
    answer=discord.ui.TextInput(label="Your Answer",placeholder="Enter answer...")
    def __init__(self, hv, dm_msg): super().__init__(); self.hv=hv; self.dm_msg=dm_msg

    async def on_submit(self, i: discord.Interaction):
        correct=(self.answer.value.strip()==self.hv.answer)
        if correct: self.hv.passed+=1
        self.hv.stage+=1
        if self.hv.stage>=self.hv.required:
            await i.response.defer(); await self._finish(i); return
        self.hv._gen()
        status="✅ Correct!" if correct else f"❌ Wrong! Answer was `{self.hv.answer}`"
        e=discord.Embed(title=f"💻 Hacking — Stage {self.hv.stage+1}/{self.hv.required}",
            description=(f"{status}\n\n**Stage {self.hv.stage+1}:** {self.hv.challenge}\n\n"
                         f"Progress: {'✅'*self.hv.passed}{'⬜'*(self.hv.required-self.hv.passed)}"),color=0x00FF41)
        sv=HackStageView(self.hv,self.dm_msg)
        await i.response.defer()
        try: await self.dm_msg.edit(embed=e,view=sv)
        except: pass

    async def _finish(self, i):
        hv=self.hv
        bonus=hv.passed/hv.required*0.20
        final=min(hv.hack_chance+bonus,HACK_MAX_CHANCE)
        roll=random.random(); success=roll<final
        target_bank=get_user_bank(hv.target_uid)
        items=get_user_items(hv.hacker_id)
        if success:
            steal=random.randint(HACK_MIN_EARN,HACK_MAX_EARN)
            steal=min(steal,target_bank['balance']); steal=max(0,steal)
            conn=db(); cur=conn.cursor()
            try:
                cur.execute("UPDATE user_bank SET balance=GREATEST(0,balance-%s) WHERE user_id=%s",(steal,hv.target_uid))
                if items.get('has_transfer'):
                    comp=datetime.now(timezone.utc)+timedelta(minutes=TRANSFER_DELAY_MINS)
                    cur.execute("INSERT INTO hack_transfers(hacker_id,amount,completes_at) VALUES(%s,%s,%s)",(hv.hacker_id,steal,comp))
                    msg=f"✅ **HACK SUCCESSFUL!** Stole **{steal:,} credits**.\n📡 Transfer arrives in **{TRANSFER_DELAY_MINS//60} hours**."
                else:
                    cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s",(steal,hv.hacker_id))
                    msg=f"✅ **HACK SUCCESSFUL!** Stole **{steal:,} credits**.\n⚠️ Get a Transfer System for safer delivery!"
                conn.commit(); _cache_invalidate(hv.hacker_id); _cache_invalidate(hv.target_uid)
            except Exception as e:
                conn.rollback(); msg=f"❌ Hack succeeded but error: {e}"
            finally: release(conn)
            color=0x00FF41
        else:
            hacker_bank=get_user_bank(hv.hacker_id)
            penalty=int(hacker_bank['balance']*HACK_PENALTY_PCT)
            conn=db(); cur=conn.cursor()
            try:
                if penalty>0:
                    cur.execute("UPDATE user_bank SET balance=GREATEST(0,balance-%s) WHERE user_id=%s",(penalty,hv.hacker_id))
                    cur.execute("UPDATE user_bank SET balance=balance+%s WHERE user_id=%s",(penalty,hv.target_uid))
                increment_hack_fail(hv.hacker_id,hv.target_bank_id)
                if items.get('has_dlg'):
                    fc=get_hack_progress(hv.hacker_id,hv.target_bank_id)
                    cur.execute("INSERT INTO data_cards(owner_id,target_bank_id,fail_count_snapshot) VALUES(%s,%s,%s)",(hv.hacker_id,hv.target_bank_id,fc))
                conn.commit(); _cache_invalidate(hv.hacker_id)
            except Exception as e:
                conn.rollback()
            finally: release(conn)
            pen_str=f"\n💸 **{penalty:,} credits** deducted from your bank!" if penalty>0 else ""
            dlg_str="\n💾 **Data Card** earned! +1% success." if items.get('has_dlg') else ""
            msg=f"❌ **HACK FAILED!** Security blocked you.{pen_str}{dlg_str}"; color=0xFF0000
        e=discord.Embed(title="💻 Hacking Result",description=msg,color=color)
        e.add_field(name="🎯 Chance",value=f"{final*100:.1f}%",inline=True)
        e.add_field(name="🎲 Roll",value=f"{roll*100:.1f}%",inline=True)
        e.add_field(name="✅ Stages",value=f"{hv.passed}/{hv.required}",inline=True)
        try: await self.dm_msg.edit(embed=e,view=None)
        except: pass

class HackStageView(discord.ui.View):
    def __init__(self, hv, dm_msg): super().__init__(timeout=120); self.hv=hv; self.dm_msg=dm_msg
    @discord.ui.button(label="💻 Enter Answer",style=discord.ButtonStyle.green)
    async def enter(self,i: discord.Interaction,b: discord.ui.Button):
        if i.user.id!=self.hv.hacker_id: await i.response.send_message("Not your terminal.",ephemeral=True); return
        await i.response.send_modal(HackInputModal(self.hv,self.dm_msg))

# ─── Black Market Buy View ─────────────────────────────────────────────────────
class BlackMarketBuyView(discord.ui.View):
    def __init__(self, item_key, price, buyer_id):
        super().__init__(timeout=60)
        self.item_key=item_key; self.price=price; self.buyer_id=buyer_id

    @discord.ui.button(label="💰 Purchase", style=discord.ButtonStyle.green)
    async def purchase(self, i: discord.Interaction, b: discord.ui.Button):
        if i.user.id!=self.buyer_id: await i.response.send_message("Not your transaction.",ephemeral=True); return
        uid=i.user.id; bm=get_user_bm(uid)
        if not bm: await i.response.send_message("❌ Black Market expired.",ephemeral=True); self.stop(); return
        if bm["stock"].get(self.item_key,0)<=0: await i.response.send_message("❌ Out of stock!",ephemeral=True); self.stop(); return
        u=get_user(uid)
        if not u or u['credits']<self.price: await i.response.send_message(f"❌ Need **{self.price:,}** credits.",ephemeral=True); return
        item=BLACK_MARKET_ITEMS[self.item_key]
        conn=db(); cur=conn.cursor()
        try:
            cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s",(self.price,uid))
            cur.execute("INSERT INTO black_market_log(user_id,item_key,price) VALUES(%s,%s,%s)",(uid,self.item_key,self.price))
            col_map={"data_leak_generator":"has_dlg","suspicious_os":"has_sos","transfer_system":"has_transfer","ai_machine":"has_ai_machine"}
            if self.item_key in col_map:
                cur.execute(f"UPDATE user_items SET {col_map[self.item_key]}=TRUE WHERE user_id=%s",(uid,))
            conn.commit(); _cache_invalidate(uid)
            bm["stock"][self.item_key]=bm["stock"].get(self.item_key,1)-1
        except Exception as e:
            conn.rollback(); await i.response.send_message("❌ Purchase failed.",ephemeral=True); return
        finally: release(conn)
        await i.response.send_message(f"✅ Purchased **{item['name']}**!",ephemeral=True)
        self.stop()

# ═══════════════════════════════════════════════════════════════════════════════
#  CORE LOGIC HELPERS (used by both commands and buttons)
# ═══════════════════════════════════════════════════════════════════════════════

async def _respond(target, embed=None, content=None, view=None, ephemeral=False):
    """Unified respond helper for both ctx and interaction."""
    if isinstance(target, discord.Interaction):
        try:
            if not target.response.is_done():
                await target.response.send_message(content=content or "", embed=embed, view=view, ephemeral=ephemeral)
            else:
                await target.followup.send(content=content or "", embed=embed, view=view, ephemeral=ephemeral)
        except: pass
    else:
        await target.send(content=content, embed=embed, view=view)

async def _send_balance(target, uid: int):
    u = get_user(uid)
    if not u: await _respond(target, content="❌ Profile not found."); return
    vb = get_user_bank(uid); apply_bank_interest(uid)
    pv = get_portfolio_value(uid)
    prestige_v = u.get('prestige') or 0
    wc = u.get('work_count') or 0
    rn,_,re,_ = get_job_rank(wc)
    e = discord.Embed(title=f"💳 {u['username']}'s Wallet", color=0x57F287)
    e.add_field(name="🎟️ Credits",   value=f"**{u['credits']:,}**",              inline=True)
    e.add_field(name="🏦 Bank",       value=f"**{vb['balance']:,}**",              inline=True)
    e.add_field(name="🪙 Coins",      value=f"**{u['total_coins']}**",             inline=True)
    e.add_field(name="📈 Portfolio",  value=f"**${pv:.4f}**",                      inline=True)
    e.add_field(name="⭐ Prestige",   value=f"**{prestige_v}** (×{prestige_multiplier(prestige_v):.1f})", inline=True)
    e.add_field(name=f"{re} Job",     value=f"**{rn}** (#{wc})",                   inline=True)
    view = BalanceView(uid)
    await _respond(target, embed=e, view=view)

async def _send_vbank(target, uid: int):
    apply_bank_interest(uid)
    vb = get_user_bank(uid); u = get_user(uid)
    if not u: await _respond(target, content="❌ Profile not found."); return
    now  = datetime.now(timezone.utc)
    last = vb['last_interest']
    if last.tzinfo is None: last=last.replace(tzinfo=timezone.utc)
    nxt  = last + timedelta(minutes=BANK_INTEREST_MINS)
    diff = nxt - now; secs=max(0,int(diff.total_seconds())); m,s=divmod(secs,60)
    e = discord.Embed(title=f"🏦 {u['username']}'s Bank", color=0x57F287)
    e.add_field(name="🏦 Balance",    value=f"**{vb['balance']:,}**",    inline=True)
    e.add_field(name="💳 Wallet",     value=f"**{u['credits']:,}**",     inline=True)
    e.add_field(name="📈 Rate",       value="+5% / 10 min",               inline=True)
    e.add_field(name="⏰ Next Int.",  value=f"In **{m}m {s}s**",         inline=True)
    await _respond(target, embed=e, view=VBankView(uid))

async def _do_deposit(target, uid: int, amount_str: str):
    u = get_user(uid)
    if not u: await _respond(target, content="❌ Profile not found."); return
    amt = u['credits'] if amount_str.lower()=='all' else (int(amount_str) if amount_str.isdigit() else None)
    if amt is None: await _respond(target, content="❌ Invalid amount."); return
    if amt<=0: await _respond(target, content="❌ Amount must be positive."); return
    if amt>u['credits']: await _respond(target, content=f"❌ Only have **{u['credits']:,}**."); return
    apply_bank_interest(uid)
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s",(amt,uid))
        cur.execute("INSERT INTO user_bank(user_id,balance,last_interest) VALUES(%s,%s,NOW()) ON CONFLICT(user_id) DO UPDATE SET balance=user_bank.balance+%s",(uid,amt,amt))
        conn.commit(); _cache_invalidate(uid)
    except: conn.rollback(); await _respond(target, content="❌ Error."); return
    finally: release(conn)
    vb=get_user_bank(uid)
    e=discord.Embed(title="🏦 Deposit!",color=0x57F287)
    e.add_field(name="💰 Deposited",   value=f"**{amt:,}**",          inline=True)
    e.add_field(name="🏦 New Balance", value=f"**{vb['balance']:,}**", inline=True)
    await _respond(target, embed=e)

async def _do_withdraw(target, uid: int, amount_str: str):
    apply_bank_interest(uid); vb=get_user_bank(uid)
    amt = vb['balance'] if amount_str.lower()=='all' else (int(amount_str) if amount_str.isdigit() else None)
    if amt is None: await _respond(target, content="❌ Invalid amount."); return
    if amt<=0: await _respond(target, content="❌ Amount must be positive."); return
    if amt>vb['balance']: await _respond(target, content=f"❌ Only have **{vb['balance']:,}** in bank."); return
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s",(amt,uid))
        cur.execute("UPDATE user_bank SET balance=balance-%s WHERE user_id=%s",(amt,uid))
        conn.commit(); _cache_invalidate(uid)
    except: conn.rollback(); await _respond(target, content="❌ Error."); return
    finally: release(conn)
    u=get_user(uid)
    e=discord.Embed(title="🏦 Withdraw!",color=0x57F287)
    e.add_field(name="💰 Withdrawn", value=f"**{amt:,}**",         inline=True)
    e.add_field(name="💳 Wallet",    value=f"**{u['credits']:,}**", inline=True)
    await _respond(target, embed=e)

async def _send_inventory(target, uid: int, page: int=1):
    await _send_inventory_view(target, uid, uid, page, "value_desc", None, None, None)

async def _send_cooldowns(target, uid: int):
    u = get_user(uid)
    if not u: await _respond(target, content="❌ Profile not found."); return
    now_ts = int(time.time()); now_dt = datetime.now(timezone.utc)
    def fmt(last_ts, cd_h):
        rem = int(cd_h*3600) - (now_ts-(last_ts or 0))
        if rem<=0: return "✅ Ready!"
        h,r=divmod(rem,3600); m,s=divmod(r,60)
        return f"⏳ {h}h {m}m" if h>0 else (f"⏳ {m}m {s}s" if m>0 else f"⏳ {s}s")
    today=now_dt.date(); ld=u.get('last_daily')
    if not ld or ld<today: daily_s="✅ Ready!"
    else:
        tom=datetime.combine(today+timedelta(days=1),datetime.min.time(),tzinfo=timezone.utc)
        diff=tom-now_dt; h,r=divmod(int(diff.total_seconds()),3600); m=r//60
        daily_s=f"⏳ {h}h {m}m"
    wc=u.get('work_count') or 0; pv=u.get('prestige') or 0
    rn,rs,re,ri=get_job_rank(wc); prev=JOB_RANKS[ri][1]
    wm=min(1.0+(wc-prev)*0.02,1.5); np=int(rs*wm*prestige_multiplier(pv))
    e=discord.Embed(title=f"⏱️ {u['username']}'s Cooldowns",color=0x5865F2)
    e.add_field(name="📅 Daily",        value=daily_s,                                 inline=True)
    e.add_field(name="💼 Work",         value=fmt(u.get('last_work_ts'),WORK_COOLDOWN_H),inline=True)
    e.add_field(name="🦹 Rob",          value=fmt(u.get('last_rob_ts'),ROB_COOLDOWN_H), inline=True)
    e.add_field(name="🎰 Gamble",       value="✅ No CD",                              inline=True)
    e.add_field(name=f"{re} Next Pay",  value=f"~{np:,} cr ({rn})",                    inline=True)
    if _market_prices["last_updated"]:
        nxt=_market_prices["last_updated"]+timedelta(minutes=20)-now_dt
        sc=max(0,int(nxt.total_seconds())); mm,ss=divmod(sc,60)
        e.add_field(name="📈 Market",   value=f"Refresh in **{mm}m {ss}s**",           inline=True)
    items=get_user_items(uid)
    if items.get('has_cognitive_machine'):
        el=now_ts-(items.get('last_cognitive_ts') or 0)
        e.add_field(name="🧠 Cognitive", value="✅ Ready" if el>=1800 else f"⏳ {(1800-el)//60}m {(1800-el)%60}s", inline=True)
    if items.get('has_ai_machine'):
        el=now_ts-(items.get('last_ai_ts') or 0)
        e.add_field(name="🤖 AI Machine",value="✅ Ready" if el>=900  else f"⏳ {(900-el)//60}m {(900-el)%60}s",   inline=True)
    bm=get_user_bm(uid)
    if bm:
        tl=bm["expires"]-now_dt; mm=int(tl.total_seconds()//60); ss=int(tl.total_seconds()%60)
        e.add_field(name="🌑 Black Market",value=f"✅ Active! {mm}m {ss}s left",        inline=True)
    else:
        e.add_field(name="🌑 Black Market",value="❌ Inactive (10%/2h)",                 inline=True)
    await _respond(target, embed=e, view=QuickNav(uid))

async def _send_shop(target, uid: int):
    cc=get_dynamic_crate_cost()
    e=discord.Embed(title="🛒 CoinVault Shop",color=0xEB459E)
    e.description=(f"Use the buttons below to buy, or type commands.\n"
                   f"Current crate cost: **{cc} cr** (adjusts with economy)\n\n")
    e.add_field(name=f"📦 1 Crate — {cc:,} cr",           value="Open a coin crate (10% bank fee)",    inline=False)
    e.add_field(name=f"📦×3 Crates — {int(cc*3*0.90):,} cr", value="3 crates (10% discount)",         inline=False)
    e.add_field(name=f"📦×5 Crates — {int(cc*5*0.84):,} cr", value="5 crates (16% discount)",         inline=False)
    e.add_field(name="🎲 Float Changer — 320 cr",          value="`-buy float_changer <coin_id>`",      inline=False)
    e.add_field(name="⚡ Market Trigger — 250 cr",          value="Trigger a boom/bust event",          inline=False)
    e.add_field(name="🛠️ Workshop — 2,000 cr",             value="Required for crafting items",         inline=False)
    e.add_field(name="🧠 Cognitive Machine — 6,000 cr",    value="Auto-work 30min @ 0.75× salary",      inline=False)
    e.add_field(name="✨ Polish — 150 cr",                  value="`-buy polish <coin_id>` — upgrade status", inline=False)
    e.add_field(name="✏️ Rename — 200 cr",                 value="`-buy rename <coin_id> <name>`",      inline=False)
    e.set_footer(text="🌑 AI Machine only from Black Market • -help for all commands")
    await _respond(target, embed=e, view=ShopView(uid))

async def _do_buy_crate(target, uid: int, count: int):
    disc={1:1.0,3:0.90,5:0.84}.get(count,1.0)
    base=get_dynamic_crate_cost()
    cost=int(base*count*disc)
    u=get_user(uid)
    if not u or u['credits']<cost:
        bal=u['credits'] if u else 0
        await _respond(target, content=f"❌ Need **{cost:,}** credits. You have **{bal:,}**."); return
    bank_cut=max(1,int(cost*CRATE_FEE_PCT))
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s",(cost,uid))
        cur.execute("UPDATE bank SET total=total+%s WHERE id=1",(bank_cut,))
        opened=[]
        for _ in range(count):
            c=generate_coin()
            cur.execute("""INSERT INTO coins(owner_id,material,variant,status,float,serial,
                base_value,mat_mult,var_mult,sta_mult,flt_mult,ser_mult,total_mult,value)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (uid,c['material'],c['variant'],c['status'],c['float'],c['serial'],
                 c['base_value'],c['mat_mult'],c['var_mult'],c['sta_mult'],c['flt_mult'],
                 c['ser_mult'],c['total_mult'],c['value']))
            cur.execute("SELECT lastval() as id"); c['id']=cur.fetchone()['id']
            opened.append(c)
        sync_coin_count(uid,cur); conn.commit(); _cache_invalidate(uid)
    except Exception as ex:
        conn.rollback(); await _respond(target, content=f"❌ Error: {str(ex)[:80]}"); return
    finally: release(conn)

    if count==1:
        c=opened[0]; mkt=get_market_price(c); tier=tier_emoji(mkt)
        e=discord.Embed(title=f"📦 Crate Opened! {tier}",color=0xFFD700)
        e.add_field(name="🪙 Coin",  value=f"**{c['variant']} {c['material']} Coin** `#{str(c['serial']).zfill(4)}`",inline=False)
        e.add_field(name="📊 Attrs", value=(f"Material: **{c['material']}** ×{c['mat_mult']}\n"
            f"Variant: **{c['variant']}** ×{c['var_mult']}\nStatus: **{c['status']}** ×{c['sta_mult']}\n"
            f"Float: **{c['float']}** ×{c['flt_mult']}\nSerial: #{str(c['serial']).zfill(4)} ×{c['ser_mult']}"),inline=True)
        e.add_field(name="💰 Value", value=f"Base: **${c['base_value']:.2f}**\nStored: **${c['value']:.4f}**\n📈 Market: **${mkt:.4f}**",inline=True)
        rl=rarity_label(coin_rarity_score(c))
        e.add_field(name="✨ Rarity", value=f"**{rl}**",inline=False)
        e.set_footer(text=f"Coin ID: #{c['id']} • Credits left: {u['credits']-cost:,}")
        await _respond(target, embed=e, view=QuickNav(uid))
    else:
        e=discord.Embed(title=f"📦 {count} Crates Opened!",color=0xFFD700)
        lines=[]; tv=tm=0.0; best=max(opened,key=lambda c:get_market_price(c))
        for c in opened:
            mkt=get_market_price(c); tier=tier_emoji(mkt); tv+=c['value']; tm+=mkt
            lines.append(f"{tier} `#{c['id']}` **{c['variant']} {c['material']}** — 📈${mkt:.4f} | {rarity_label(coin_rarity_score(c))}")
        e.description="\n".join(lines)
        e.add_field(name="📊 Summary",value=(f"Total Base: **${tv:.4f}** | Market: **${tm:.4f}**\n"
            f"Best: `#{best['id']}` — 📈${get_market_price(best):.4f}"),inline=False)
        e.set_footer(text=f"Credits left: {u['credits']-cost:,} • Fee: {bank_cut:,} cr")
        await _respond(target, embed=e, view=QuickNav(uid))

async def _do_sell_coin(target, uid: int, cid: int):
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT * FROM coins WHERE id=%s AND owner_id=%s",(cid,uid))
        c=cur.fetchone()
        if not c: await _respond(target, content=f"❌ Coin #{cid} not in your inventory."); return
        cur.execute("SELECT id FROM auctions WHERE coin_id=%s AND status='active'",(cid,))
        if cur.fetchone(): await _respond(target, content=f"❌ Coin #{cid} is in an active auction."); return
        mkt=get_market_price(c); cr=coin_value_to_credits(mkt)
        cur.execute("DELETE FROM coins WHERE id=%s",(cid,))
        cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s",(cr,uid))
        cur.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,'sell_coin')",(uid,cr))
        sync_coin_count(uid,cur); conn.commit()
        record_material_sale(c['material']); log_material_sale_db(c['material'])
        _cache_invalidate(uid)
    except Exception as ex:
        conn.rollback(); await _respond(target, content="❌ Error. Try again."); return
    finally: release(conn)
    name=coin_name(c); ev=get_event_mult(c['material'])
    ev_tag=" 🚀" if ev>1.0 else (" 💥" if ev<1.0 else "")
    e=discord.Embed(title="💸 Coin Sold!",color=0x57F287)
    e.description=(f"Sold **{name}** `#{str(c['serial']).zfill(4)}`\n"
                   f"📈 Market: **${mkt:.4f}** → **{cr:,} credits**{ev_tag}")
    await _respond(target, embed=e, view=QuickNav(uid))

async def _do_sellall(target, uid: int):
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT c.* FROM coins c WHERE c.owner_id=%s AND c.id NOT IN (SELECT coin_id FROM auctions WHERE status='active')",(uid,))
        coins=cur.fetchall()
        if not coins: await _respond(target, content="🎒 No sellable coins."); return
        total_cr=sum(coin_value_to_credits(get_market_price(c)) for c in coins)
        ids=[c['id'] for c in coins]
        cur.execute("DELETE FROM coins WHERE id=ANY(%s)",(ids,))
        cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s",(total_cr,uid))
        cur.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,'sellall')",(uid,total_cr))
        sync_coin_count(uid,cur); conn.commit()
        for c in coins: record_material_sale(c['material']); log_material_sale_db(c['material'])
        _cache_invalidate(uid)
    except: conn.rollback(); await _respond(target, content="❌ Error."); return
    finally: release(conn)
    e=discord.Embed(title="💸 Sold All!",color=0x57F287)
    e.description=f"Sold **{len(coins)}** coins for **{total_cr:,} credits** at live market prices."
    await _respond(target, embed=e, view=QuickNav(uid))

async def _do_work(target, uid: int):
    u=get_user(uid); items=get_user_items(uid)
    if not u: await _respond(target, content="❌ Profile not found."); return
    now_ts=int(time.time()); cd=int(WORK_COOLDOWN_H*3600)
    elapsed=now_ts-(u.get('last_work_ts') or 0)
    if elapsed<cd:
        rem=cd-elapsed; h,r=divmod(rem,3600); m=r//60; s=r%60
        ts=f"**{h}h {m}m**" if h>0 else (f"**{m}m {s}s**" if m>0 else f"**{s}s**")
        await _respond(target, content=f"⏳ Work again in {ts}."); return
    wc=u.get('work_count') or 0; pv=u.get('prestige') or 0
    rn,rs,re,ri=get_job_rank(wc); jt=JOB_TITLES.get(rn,rn); pm=JOB_RANKS[ri][1]
    wm=min(1.0+(wc-pm)*0.02,1.5); bp=min(int(rs*wm),5000); earned=int(bp*prestige_multiplier(pv))
    action=random.choice(WORK_ACTIONS); nwc=wc+1
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("UPDATE users SET credits=credits+%s,last_work_ts=%s,work_count=%s WHERE user_id=%s",(earned,now_ts,nwc,uid))
        cur.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,'work')",(uid,earned))
        conn.commit(); _cache_invalidate(uid)
    except: conn.rollback(); await _respond(target, content="❌ Error."); return
    finally: release(conn)
    nrn,_,nre,nri=get_job_rank(nwc); ranked_up=nri>ri
    e=discord.Embed(title="💼 Work Complete!",color=0x57F287)
    e.description=f"{re} **{jt}** {action} and earned **{earned:,} credits**!"
    e.add_field(name="📊 Job",value=f"{rn} (#{nwc})",inline=True)
    e.add_field(name="💵 Pay",value=f"{earned:,} cr",inline=True)
    if pv>0: e.add_field(name="⭐ Prestige",value=f"×{prestige_multiplier(pv):.1f}",inline=True)
    nxt=get_next_job_rank(nwc)
    e.set_footer(text=f"Next: {nxt[3] if nxt else re} {nxt[0] if nxt else rn} in {(nxt[1]-nwc) if nxt else 0} job(s)" if nxt else f"MAX RANK: {re} {rn}")
    await _respond(target, embed=e, view=QuickNav(uid))
    if ranked_up:
        njt=JOB_TITLES.get(nrn,nrn)
        re2=discord.Embed(title=f"🎉 PROMOTED! {nre} {nrn}",
            description=f"**{u['username']}** is now **{njt}**!",color=0xFFD700)
        await _respond(target, embed=re2)

async def _do_daily(target, uid: int):
    u=get_user(uid)
    if not u: await _respond(target, content="❌ Profile not found."); return
    today=datetime.now(timezone.utc).date(); ld=u.get('last_daily')
    if ld and ld==today:
        tom=datetime.combine(today+timedelta(days=1),datetime.min.time(),tzinfo=timezone.utc)
        diff=tom-datetime.now(timezone.utc); h,r=divmod(int(diff.total_seconds()),3600); m=r//60
        await _respond(target, content=f"⏳ Already claimed! Come back in **{h}h {m}m**."); return
    yest=today-timedelta(days=1)
    streak=((u.get('daily_streak') or 0)+1) if ld==yest else 1
    bonus=min(streak-1,7)*DAILY_STREAK_BONUS; pv=u.get('prestige') or 0
    total=int((DAILY_CREDITS+bonus)*prestige_multiplier(pv))
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("UPDATE users SET credits=credits+%s,last_daily=%s,daily_streak=%s WHERE user_id=%s",(total,today,streak,uid))
        cur.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,'daily')",(uid,total))
        conn.commit(); _cache_invalidate(uid)
    except: conn.rollback(); await _respond(target, content="❌ Error."); return
    finally: release(conn)
    e=discord.Embed(title="📅 Daily Claimed!",color=0x57F287)
    e.add_field(name="💰 Credits",value=f"**{total:,}**",          inline=True)
    e.add_field(name="🔥 Streak", value=f"**{streak}** day(s)",     inline=True)
    if bonus: e.add_field(name="🎁 Bonus",value=f"+{bonus}",        inline=True)
    e.set_footer(text="Come back tomorrow! Max bonus at day 8.")
    await _respond(target, embed=e, view=QuickNav(uid))

async def _do_market_trigger(target, uid: int):
    cost=SHOP_ITEMS['market_trigger']['cost']; u=get_user(uid)
    if not u or u['credits']<cost:
        bal=u['credits'] if u else 0
        await _respond(target, content=f"❌ Need **{cost:,}** credits. You have **{bal:,}**."); return
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s",(cost,uid))
        conn.commit(); _cache_invalidate(uid)
    finally: release(conn)
    event=generate_market_prices()
    if event:
        ev_type,mat,mult=event; icon="📈" if ev_type=="boom" else "📉"
        await _respond(target, content=f"⚡ **Market Trigger!** {icon} **{ev_type.upper()}** on **{mat}** ×{mult} for {EVENT_DURATION_MINS}min!")
    else:
        await _respond(target, content="⚡ **Market Trigger!** Prices refreshed, no event this cycle.")

# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    init_db(); generate_market_prices()
    for task in [auction_checker,daily_bank_distribution,update_market_prices,
                 black_market_spawner,auto_worker_task,hack_transfer_checker]:
        if not task.is_running(): task.start()
    print(f"✅ CoinVault ready as {bot.user}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Missing argument. Use `-help` for usage.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Bad argument. Use `-help`.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    elif isinstance(error, commands.CommandInvokeError):
        print(f"CommandInvokeError in {ctx.command}: {error.original}")
        await ctx.send("❌ An error occurred. Please try again.")
    else:
        print(f"Unhandled error: {error}")

@bot.command()
async def help(ctx):
    uid=ctx.author.id
    e=discord.Embed(title="🪙 CoinVault — Commands",color=0x5865F2)
    e.add_field(name="💰 Economy",value=(
        "`-balance` / `-bal` — Wallet & stats (with action buttons)\n"
        "`-daily` — Claim daily (streak bonuses)\n"
        "`-work` — Work for credits (30min CD)\n"
        "`-rob @user` — Rob someone (1h CD, 45% chance)\n"
        "`-gamble <amount>` — Coinflip\n"
        "`-slots <amount>` — Slot machine\n"
        "`-prestige` — Spend 5,000 cr (+10% all earnings)\n"
        "`-cd` — Cooldowns overview\n"
    ),inline=False)
    e.add_field(name="🏦 Bank",value=(
        "`-vbank` — Virtual bank (5%/10min interest)\n"
        "`-deposit <amount|all>` — Deposit credits\n"
        "`-withdraw <amount|all>` — Withdraw credits\n"
        "`-mybankid` — Your secret bank ID (DM only)\n"
    ),inline=False)
    e.add_field(name="🛒 Shop & Items",value=(
        "`-shop` — Browse with quick-buy buttons\n"
        "`-buy crate [all]` — Open crates\n"
        "`-buy float_changer <id>` — 320 cr re-roll float\n"
        "`-buy polish <id>` — 150 cr upgrade status\n"
        "`-buy rename <id> <name>` — 200 cr rename\n"
        "`-buy workshop` — 2,000 cr crafting\n"
        "`-buy cognitive_machine` — 6,000 cr auto-work\n"
        "`-worktoggle cognitive/ai` — Toggle machines\n"
        "`-items` — View owned items\n"
    ),inline=False)
    e.add_field(name="🎒 Coins & Inventory",value=(
        "`-inventory [page]` / `-inv` — Coins with filters/sort buttons\n"
        "`-coin <id>` — Detailed coin view\n"
        "`-sell <id>` — Sell a coin (confirm prompt)\n"
        "`-sellall` — Sell all coins (confirm prompt)\n"
        "`-privacy on/off` — Toggle inventory visibility\n"
    ),inline=False)
    e.add_field(name="🤝 Trading",value=(
        "`-trade @user [coin_ids] [credits:<n>]` — Offer trade\n"
        "`-trades` — Pending trades\n"
        "`-market [page]` — Marketplace with filters/sort\n"
        "`-auction <coin_id> <price> [hours]` — List auction\n"
        "`-bid <auction_id>` — Bid\n"
        "`-myauctions` — Your listings\n"
        "`-cancelauction <id>` — Cancel listing\n"
    ),inline=False)
    e.add_field(name="📊 Stats & Market",value=(
        "`-profile [@user]` — Profile card\n"
        "`-lb` — Leaderboard (5 modes, toggle button)\n"
        "`-prices [material/float/status]` — Live prices\n"
        "`-economy` — Economy overview\n"
        "`-coinprice <id>` — Live coin price\n"
        "`-jobrank [@user]` — Career progress\n"
        "`-jobladder` — All job ranks\n"
    ),inline=False)
    e.add_field(name="🌑 Black Market (DM · role required)",value=(
        "`-blackmarket` — View your personal BM\n"
        "`-bmbuy <item_key>` — Purchase item\n"
        "`-usetracker @user` — Reveal bank ID (one-use)\n"
    ),inline=False)
    e.add_field(name="🔓 Hacking (DM · role required)",value=(
        "Requires: Suspicious OS + Data Leak Gen + Transfer System\n"
        "`-hack <bank_id>` — Hack a bank (2h transfer delay)\n"
        "`-hackinventory` — Hack items & data cards\n"
        "`-hacktransfers` — Pending transfers\n"
    ),inline=False)
    e.set_footer(text="Rob CD: 1h • Hack transfer: 2h • Most commands have interactive buttons!")
    await ctx.send(embed=e, view=QuickNav(uid))

@bot.command(aliases=['bal','wallet'])
async def balance(ctx):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    await _send_balance(ctx, uid)

@bot.command()
async def daily(ctx):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    await _do_daily(ctx, uid)

@bot.command()
async def work(ctx):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    await _do_work(ctx, uid)

@bot.command()
async def worktoggle(ctx, machine: str=None):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    items=get_user_items(uid)
    if not machine: await ctx.send("❌ Usage: `-worktoggle cognitive` or `-worktoggle ai`"); return
    m=machine.lower()
    if m in ("cognitive","cog"):
        if not items.get('has_cognitive_machine'): await ctx.send("❌ You don't own a Cognitive Machine."); return
        ns=not items.get('cognitive_enabled',False)
        conn=db(); cur=conn.cursor()
        try: cur.execute("UPDATE user_items SET cognitive_enabled=%s WHERE user_id=%s",(ns,uid)); conn.commit(); _cache_invalidate(uid)
        finally: release(conn)
        await ctx.send(f"🧠 Cognitive Machine: **{'ON 🟢' if ns else 'OFF 🔴'}**")
    elif m in ("ai","aimachine"):
        if not items.get('has_ai_machine'): await ctx.send("❌ You don't own an AI Machine."); return
        ns=not items.get('ai_enabled',False)
        conn=db(); cur=conn.cursor()
        try: cur.execute("UPDATE user_items SET ai_enabled=%s WHERE user_id=%s",(ns,uid)); conn.commit(); _cache_invalidate(uid)
        finally: release(conn)
        await ctx.send(f"🤖 AI Machine: **{'ON 🟢' if ns else 'OFF 🔴'}**")
    else: await ctx.send("❌ Use `cognitive` or `ai`.")

@bot.command()
async def items(ctx, member: discord.Member=None):
    target=member or ctx.author; uid=target.id; ensure_user(uid,str(target))
    inv=get_user_items(uid)
    tick=lambda h: "✅" if h else "❌"
    e=discord.Embed(title=f"🎒 {target.display_name}'s Items",color=0x5865F2)
    e.add_field(name="🛠️ Workshop",        value=tick(inv.get('has_workshop')),          inline=True)
    e.add_field(name="🧠 Cognitive",       value=tick(inv.get('has_cognitive_machine')), inline=True)
    e.add_field(name="🤖 AI Machine",      value=tick(inv.get('has_ai_machine')),        inline=True)
    e.add_field(name="💾 Data Leak Gen",   value=tick(inv.get('has_dlg')),               inline=True)
    e.add_field(name="💻 Suspicious OS",   value=tick(inv.get('has_sos')),               inline=True)
    e.add_field(name="📡 Transfer System", value=tick(inv.get('has_transfer')),          inline=True)
    mlines=[]
    if inv.get('has_cognitive_machine'): mlines.append(f"🧠 Cognitive: {'ON 🟢' if inv.get('cognitive_enabled') else 'OFF 🔴'}")
    if inv.get('has_ai_machine'):        mlines.append(f"🤖 AI: {'ON 🟢' if inv.get('ai_enabled') else 'OFF 🔴'}")
    if mlines: e.add_field(name="⚙️ Status",value="\n".join(mlines),inline=False)
    e.set_footer(text="-worktoggle cognitive/ai to toggle machines")
    await ctx.send(embed=e)

@bot.command()
async def mybankid(ctx):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author)); vb=get_user_bank(uid)
    bid=vb.get('bank_id') or "Not generated yet."
    try:
        await ctx.author.send(f"🏦 **Your Bank ID:**\n```\n{bid}\n```\n⚠️ Keep private! Hackers use this.")
        await ctx.send("✅ Bank ID sent to your DMs.")
    except discord.Forbidden:
        await ctx.send(f"🏦 **Bank ID:**\n||`{bid}`||\n*(Enable DMs for privacy)*")

@bot.command(aliases=['vb','mybank'])
async def vbank(ctx):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    await _send_vbank(ctx, uid)

@bot.command()
async def deposit(ctx, amount: str):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    await _do_deposit(ctx, uid, amount)

@bot.command()
async def withdraw(ctx, amount: str):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    await _do_withdraw(ctx, uid, amount)

@bot.command()
async def shop(ctx):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    await _send_shop(ctx, uid)

@bot.command()
async def buy(ctx, item: str=None, *args):
    if not item: await ctx.send("❌ Usage: `-buy <item>`. See `-shop`."); return
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    item=item.lower()

    if item in ("crate","crate_x3","crate_x5"):
        n={"crate":1,"crate_x3":3,"crate_x5":5}[item]
        disc={"crate":1.0,"crate_x3":0.90,"crate_x5":0.84}[item]
        base=get_dynamic_crate_cost(); unit=int(base*n*disc)
        u=get_user(uid)
        repeat=1
        if args and args[0].lower()=='all':
            repeat=min(max(1,u['credits']//unit),25)
            if u['credits']<unit: await ctx.send(f"❌ Need **{unit:,}** credits."); return
        await _do_buy_crate(ctx, uid, n*repeat)
        return

    if item=="float_changer":
        if not args: await ctx.send("❌ Usage: `-buy float_changer <coin_id>`"); return
        try: cid=int(args[0])
        except: await ctx.send("❌ Invalid coin ID."); return
        cost=SHOP_ITEMS['float_changer']['cost']; u=get_user(uid)
        if not u or u['credits']<cost: await ctx.send(f"❌ Need **{cost:,}** credits."); return
        conn=db(); cur=conn.cursor()
        try:
            cur.execute("SELECT * FROM coins WHERE id=%s AND owner_id=%s",(cid,uid))
            c=cur.fetchone()
            if not c: await ctx.send(f"❌ Coin #{cid} not in inventory."); return
            of=c['float']; om=c['flt_mult']
            nf,nm=weighted_choice(FLOATS)
            nt=round(c['mat_mult']*c['var_mult']*c['sta_mult']*nm*c['ser_mult'],4)
            nv=round(c['base_value']*nt,4)
            cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s",(cost,uid))
            cur.execute("UPDATE coins SET float=%s,flt_mult=%s,total_mult=%s,value=%s WHERE id=%s",(nf,nm,nt,nv,cid))
            conn.commit(); _cache_invalidate(uid)
        except: conn.rollback(); await ctx.send("❌ Error."); return
        finally: release(conn)
        arr="📈" if nm>om else "📉"
        await ctx.send(embed=discord.Embed(title="🎲 Float Changed!",color=0x57F287,
            description=f"Coin `#{cid}`: **{of}** ×{om} → {arr} **{nf}** ×{nm}\nNew value: **${nv:.4f}**"))
        return

    if item=="market_trigger":
        await _do_market_trigger(ctx, uid); return

    if item=="workshop":
        cost=SHOP_ITEMS['workshop']['cost']; u=get_user(uid); inv=get_user_items(uid)
        if not u or u['credits']<cost: await ctx.send(f"❌ Need **{cost:,}** credits."); return
        if inv.get('has_workshop'): await ctx.send("❌ Already own a Workshop."); return
        conn=db(); cur=conn.cursor()
        try:
            cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s",(cost,uid))
            cur.execute("UPDATE user_items SET has_workshop=TRUE WHERE user_id=%s",(uid,))
            conn.commit(); _cache_invalidate(uid)
        except: conn.rollback(); await ctx.send("❌ Error."); return
        finally: release(conn)
        await ctx.send("🛠️ **Crafting Workshop** purchased!")
        return

    if item=="cognitive_machine":
        cost=SHOP_ITEMS['cognitive_machine']['cost']; u=get_user(uid); inv=get_user_items(uid)
        if not u or u['credits']<cost: await ctx.send(f"❌ Need **{cost:,}** credits."); return
        if inv.get('has_cognitive_machine'): await ctx.send("❌ Already own a Cognitive Machine."); return
        conn=db(); cur=conn.cursor()
        try:
            cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s",(cost,uid))
            cur.execute("UPDATE user_items SET has_cognitive_machine=TRUE WHERE user_id=%s",(uid,))
            conn.commit(); _cache_invalidate(uid)
        except: conn.rollback(); await ctx.send("❌ Error."); return
        finally: release(conn)
        await ctx.send("🧠 **Cognitive Machine** purchased! Toggle with `-worktoggle cognitive`.")
        return

    if item=="polish":
        if not args: await ctx.send("❌ Usage: `-buy polish <coin_id>`"); return
        try: cid=int(args[0])
        except: await ctx.send("❌ Invalid coin ID."); return
        cost=SHOP_ITEMS['polish']['cost']; u=get_user(uid)
        if not u or u['credits']<cost: await ctx.send(f"❌ Need **{cost:,}** credits."); return
        conn=db(); cur=conn.cursor()
        try:
            cur.execute("SELECT * FROM coins WHERE id=%s AND owner_id=%s",(cid,uid))
            c=cur.fetchone()
            if not c: await ctx.send(f"❌ Coin #{cid} not found."); return
            cs=c['status']
            if cs not in STATUS_ORDER: await ctx.send("❌ Can't polish this coin."); return
            idx=STATUS_ORDER.index(cs)
            if idx>=len(STATUS_ORDER)-1: await ctx.send("❌ Already at max (**Stunning**)."); return
            ns2=STATUS_ORDER[idx+1]; nsm=next(m for n,m,_ in STATUSES if n==ns2)
            nt=round(c['mat_mult']*c['var_mult']*nsm*c['flt_mult']*c['ser_mult'],4)
            nv=round(c['base_value']*nt,4)
            cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s",(cost,uid))
            cur.execute("UPDATE coins SET status=%s,sta_mult=%s,total_mult=%s,value=%s WHERE id=%s",(ns2,nsm,nt,nv,cid))
            conn.commit(); _cache_invalidate(uid)
        except: conn.rollback(); await ctx.send("❌ Error."); return
        finally: release(conn)
        await ctx.send(embed=discord.Embed(title="✨ Polished!",color=0x57F287,
            description=f"Coin `#{cid}`: **{cs}** → **{ns2}**\nNew value: **${nv:.4f}**"))
        return

    if item=="rename":
        if len(args)<2: await ctx.send("❌ Usage: `-buy rename <coin_id> <name>`"); return
        try: cid=int(args[0])
        except: await ctx.send("❌ Invalid coin ID."); return
        name=" ".join(args[1:])[:40]; cost=SHOP_ITEMS['rename']['cost']; u=get_user(uid)
        if not u or u['credits']<cost: await ctx.send(f"❌ Need **{cost:,}** credits."); return
        conn=db(); cur=conn.cursor()
        try:
            cur.execute("SELECT id FROM coins WHERE id=%s AND owner_id=%s",(cid,uid))
            if not cur.fetchone(): await ctx.send(f"❌ Coin #{cid} not found."); return
            cur.execute("UPDATE users SET credits=credits-%s WHERE user_id=%s",(cost,uid))
            cur.execute("UPDATE coins SET custom_name=%s WHERE id=%s",(name,cid))
            conn.commit(); _cache_invalidate(uid)
        except: conn.rollback(); await ctx.send("❌ Error."); return
        finally: release(conn)
        await ctx.send(f"✅ Coin `#{cid}` renamed to **{name}**!")
        return

    await ctx.send(f"❌ Unknown item `{item}`. See `-shop`.")

@bot.command()
async def rob(ctx, target: discord.Member):
    if target.bot or target.id==ctx.author.id: await ctx.send("❌ Invalid target."); return
    uid=ctx.author.id; ensure_user(uid,str(ctx.author)); ensure_user(target.id,str(target))
    u=get_user(uid)
    if not u: await ctx.send("❌ Profile not found."); return
    now_ts=int(time.time()); cd=int(ROB_COOLDOWN_H*3600)
    elapsed=now_ts-(u.get('last_rob_ts') or 0)
    if elapsed<cd:
        rem=cd-elapsed; h,r=divmod(rem,3600); m=r//60
        await ctx.send(f"⏳ Lay low! Rob again in **{h}h {m}m**."); return
    t=get_user(target.id)
    if not t or t['credits']<50: await ctx.send(f"❌ **{target.display_name}** needs 50+ credits."); return
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("UPDATE users SET last_rob_ts=%s WHERE user_id=%s",(now_ts,uid)); _cache_invalidate(uid)
        if random.random()<ROB_SUCCESS_PCT:
            steal=max(1,int(t['credits']*random.uniform(0.05,ROB_MAX_STEAL_PCT)))
            cur.execute("UPDATE users SET credits=GREATEST(0,credits-%s) WHERE user_id=%s",(steal,target.id))
            cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s",(steal,uid))
            conn.commit(); _cache_invalidate(uid); _cache_invalidate(target.id)
            e=discord.Embed(title="🦹 Robbery!",color=discord.Color.green())
            e.description=f"Snatched **{steal:,} credits** from **{target.display_name}**!"
        else:
            fine=max(1,int((u.get('credits') or 0)*ROB_FINE_PCT))
            cur.execute("UPDATE users SET credits=GREATEST(0,credits-%s) WHERE user_id=%s",(fine,uid))
            cur.execute("UPDATE bank SET total=total+%s WHERE id=1",(fine,))
            conn.commit(); _cache_invalidate(uid)
            e=discord.Embed(title="🚔 Caught!",color=discord.Color.red())
            e.description=f"Got caught — paid **{fine:,} credits** fine!"
    except: conn.rollback(); await ctx.send("❌ Error."); return
    finally: release(conn)
    await ctx.send(embed=e)

@bot.command()
async def gamble(ctx, amount: int):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author)); u=get_user(uid)
    if not u: await ctx.send("❌ Profile not found."); return
    if amount<GAMBLE_MIN: await ctx.send(f"❌ Min bet: **{GAMBLE_MIN:,}**."); return
    if amount>u['credits']: await ctx.send(f"❌ Only have **{u['credits']:,}**."); return
    e=discord.Embed(title="🪙 Coinflip",description=f"Betting **{amount:,}** — pick a side!",color=0xFEE75C)
    await ctx.send(embed=e,view=CoinflipView(uid,amount))

SLOT_SYMS=["🍒","🍊","🍋","🍇","💎","🌟","🎰"]
SLOT_PAYS={(("💎",)*3):20,(("🌟",)*3):15,(("🎰",)*3):50,(("🍇",)*3):8,(("🍒",)*3):5,(("🍊",)*3):4,(("🍋",)*3):3}

@bot.command()
async def slots(ctx, amount: int):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author)); u=get_user(uid)
    if not u: await ctx.send("❌ Profile not found."); return
    if amount<GAMBLE_MIN: await ctx.send(f"❌ Min: **{GAMBLE_MIN:,}**."); return
    if amount>u['credits']: await ctx.send(f"❌ Only have **{u['credits']:,}**."); return
    reel=tuple(random.choices(SLOT_SYMS,weights=[30,25,25,15,5,3,2],k=3))
    mult=SLOT_PAYS.get(reel,0)
    conn=db(); cur=conn.cursor()
    try:
        if mult>0:
            net=amount*mult-amount
            cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s",(net,uid))
            cur.execute("UPDATE bank SET total=total+%s WHERE id=1",(max(1,int(amount*0.02)),))
            color=discord.Color.gold(); rl=f"🎉 **{' | '.join(reel)}** — **{mult}×**! Net +**{net:,}**"
        else:
            cur.execute("UPDATE users SET credits=GREATEST(0,credits-%s) WHERE user_id=%s",(amount,uid))
            cur.execute("UPDATE bank SET total=total+%s WHERE id=1",(max(1,int(amount*0.50)),))
            color=discord.Color.red(); rl=f"💸 **{' | '.join(reel)}** — Lost **{amount:,}**."
        conn.commit(); _cache_invalidate(uid)
    except: conn.rollback(); await ctx.send("❌ Error."); return
    finally: release(conn)
    await ctx.send(embed=discord.Embed(title="🎰 Slots",description=rl,color=color))

@bot.command()
async def prestige(ctx):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author)); u=get_user(uid)
    if not u: await ctx.send("❌ Profile not found."); return
    if u['credits']<PRESTIGE_COST: await ctx.send(f"❌ Need **{PRESTIGE_COST:,}** credits."); return
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("UPDATE users SET credits=credits-%s,prestige=prestige+1 WHERE user_id=%s",(PRESTIGE_COST,uid))
        cur.execute("UPDATE bank SET total=total+%s WHERE id=1",(PRESTIGE_COST//2,))
        conn.commit(); _cache_invalidate(uid)
    except: conn.rollback(); await ctx.send("❌ Error."); return
    finally: release(conn)
    np=(u.get('prestige') or 0)+1
    await ctx.send(embed=discord.Embed(title="⭐ PRESTIGE!",color=0xFFD700,
        description=f"**{ctx.author.display_name}** reached **Prestige {np}**! Earnings ×{prestige_multiplier(np):.1f} forever."))

@bot.command(aliases=['cd'])
async def cooldown(ctx):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    await _send_cooldowns(ctx, uid)

@bot.command(aliases=['inv'])
async def inventory(ctx, *args):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    target_uid=uid; page=1
    for arg in args:
        if arg.startswith('<@'):
            mid_str=arg.strip('<@!>').strip('>')
            try:
                mid=int(mid_str)
                tu=get_user(mid)
                if tu: target_uid=mid
            except: pass
        else:
            try: page=int(arg)
            except: pass
    if target_uid!=uid:
        tu=get_user(target_uid)
        if not tu or not tu.get('inventory_public',False):
            await ctx.send("🔒 That user's inventory is private."); return
    e=discord.Embed(title="🎒 Loading...",color=0x5865F2)
    msg=await ctx.send(embed=e)
    view=InventoryView(uid,target_uid,page)
    await _send_inventory_view(msg, uid, target_uid, page, "value_desc", None, None, None)

@bot.command()
async def coin(ctx, coin_id: int):
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT c.*,u.username FROM coins c JOIN users u ON u.user_id=c.owner_id WHERE c.id=%s",(coin_id,))
        c=cur.fetchone()
    finally: release(conn)
    if not c: await ctx.send(f"❌ Coin #{coin_id} not found."); return
    mkt=get_market_price(c); tier=tier_emoji(mkt); rap=get_coin_rap(coin_id)
    rarity=coin_rarity_score(c); ev=get_event_mult(c['material']); sd=get_supply_demand_mult(c['material'])
    mat_live=_market_prices["materials"].get(c['material'],c['mat_mult'])
    flt_live=_market_prices["floats"].get(c['float'],c['flt_mult'])
    sta_live=get_status_market_mult(c)
    e=discord.Embed(title=f"{tier} Coin #{coin_id} — {coin_name(c)}",color=0xFFD700)
    e.add_field(name="Owner",   value=c['username'],inline=True)
    e.add_field(name="Serial",  value=f"#{str(c['serial']).zfill(4)}",inline=True)
    obt=c['obtained_at']; e.add_field(name="Obtained",value=obt.strftime("%Y-%m-%d") if obt else "?",inline=True)
    e.add_field(name="📊 Attributes",value=(
        f"Material: **{c['material']}** ×{c['mat_mult']} (live ×{mat_live:.3f})\n"
        f"Variant:  **{c['variant']}** ×{c['var_mult']}\n"
        f"Status:   **{c['status']}** ×{c['sta_mult']} (live ×{sta_live:.3f})\n"
        f"Float:    **{c['float']}** ×{c['flt_mult']} (live ×{flt_live:.3f})\n"
        f"Serial:   #{str(c['serial']).zfill(4)} ×{c['ser_mult']}"),inline=True)
    e.add_field(name="💰 Value",value=(
        f"Stored: **${c['value']:.4f}**\n📈 Market: **${mkt:.4f}**\n"
        f"RAP: **{'${:,.2f}'.format(rap) if rap else 'None'}**"),inline=True)
    e.add_field(name="✨ Rarity",value=f"**{rarity_label(rarity)}** (score {rarity:.2f})",inline=True)
    econ=[]
    if ev!=1.0: econ.append(f"{'🚀 BOOM' if ev>1.0 else '💥 BUST'} ×{ev}")
    if abs(sd-1.0)>0.01: econ.append(f"{'📦 Oversupply' if sd<1.0 else '🔥 Scarce'} ×{sd:.3f}")
    if econ: e.add_field(name="🌐 Market Conditions",value="\n".join(econ),inline=False)
    await ctx.send(embed=e)

@bot.command()
async def sell(ctx, coin_id: str):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    if coin_id.lower()=='all': await sellall(ctx); return
    try: cid=int(coin_id)
    except: await ctx.send("❌ Usage: `-sell <coin_id>` or `-sell all`"); return
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT * FROM coins WHERE id=%s AND owner_id=%s",(cid,uid))
        c=cur.fetchone()
        if not c: await ctx.send(f"❌ Coin #{cid} not in inventory."); return
        cur.execute("SELECT id FROM auctions WHERE coin_id=%s AND status='active'",(cid,))
        if cur.fetchone(): await ctx.send(f"❌ Coin #{cid} is in an active auction."); return
    finally: release(conn)
    mkt=get_market_price(c); cr=coin_value_to_credits(mkt)
    ev=get_event_mult(c['material']); ev_tag=" 🚀 (BOOM!)" if ev>1.0 else (" 💥 (BUST)" if ev<1.0 else "")
    e=discord.Embed(title="💸 Confirm Sale",color=0xFEE75C)
    e.description=(f"**{coin_name(c)}** `#{str(c['serial']).zfill(4)}`\n"
                   f"📈 Market: **${mkt:.4f}** → **{cr:,} credits**{ev_tag}")
    await ctx.send(embed=e, view=SellConfirmView(uid,cid,mkt,c))

@bot.command()
async def sellall(ctx):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT c.* FROM coins c WHERE c.owner_id=%s AND c.id NOT IN (SELECT coin_id FROM auctions WHERE status='active')",(uid,))
        coins=cur.fetchall()
    finally: release(conn)
    if not coins: await ctx.send("🎒 No sellable coins."); return
    total_cr=sum(coin_value_to_credits(get_market_price(c)) for c in coins)
    e=discord.Embed(title="💸 Sell All Coins?",color=0xFEE75C)
    e.description=f"**{len(coins)}** coins → **{total_cr:,} credits** at live market prices.\nThis cannot be undone!"
    await ctx.send(embed=e, view=SellAllConfirmView(uid,len(coins),total_cr))

@bot.command()
async def privacy(ctx, setting: str=None):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    if not setting or setting.lower() not in ('on','off'):
        u=get_user(uid); s='public' if u and u.get('inventory_public') else 'private'
        await ctx.send(f"🔒 Currently **{s}**. Use `-privacy on/off`."); return
    pub=setting.lower()=='on'
    conn=db(); cur=conn.cursor()
    try: cur.execute("UPDATE users SET inventory_public=%s WHERE user_id=%s",(pub,uid)); conn.commit(); _cache_invalidate(uid)
    finally: release(conn)
    await ctx.send(f"{'🔓 Inventory **public**.' if pub else '🔒 Inventory **private**.'}")

@bot.command()
async def jobrank(ctx, member: discord.Member=None):
    target=member or ctx.author; uid=target.id; ensure_user(uid,str(target))
    u=get_user(uid)
    if not u: await ctx.send("❌ Profile not found."); return
    wc=u.get('work_count') or 0
    rn,rs,re,ri=get_job_rank(wc); jt=JOB_TITLES.get(rn,rn); pm=JOB_RANKS[ri][1]
    jir=wc-pm; wm=min(1.0+jir*0.02,1.5); cs=int(rs*wm); nxt=get_next_job_rank(wc)
    e=discord.Embed(title=f"{re} {target.display_name}'s Career",color=0x5865F2)
    e.add_field(name="🏷️ Title",value=f"**{jt}**",inline=True)
    e.add_field(name="📊 Rank",value=f"**{rn}** (Tier {ri+1}/{len(JOB_RANKS)})",inline=True)
    e.add_field(name="💼 Jobs",value=f"**{wc}**",inline=True)
    e.add_field(name="💵 Salary",value=f"**{cs:,}** cr/job (×{wm:.2f})",inline=True)
    if nxt:
        nn,nm2,ns2,ne=nxt; need=nm2-wc; tot=nm2-pm; done=wc-pm
        bar="█"*int((done/tot)*10)+"░"*(10-int((done/tot)*10))
        e.add_field(name=f"⬆️ Next: {ne} {nn}",value=f"`{bar}` {done}/{tot}\n{need} jobs → **{ns2:,}** cr/job",inline=False)
    else: e.add_field(name="🏆",value="**MAX RANK!**",inline=False)
    await ctx.send(embed=e)

@bot.command()
async def jobladder(ctx):
    e=discord.Embed(title="💼 Job Ladder",color=0xFFD700)
    lines=[]
    for i,(name,min_wc,salary,emoji) in enumerate(JOB_RANKS):
        r=f"Jobs {min_wc}–{JOB_RANKS[i+1][1]-1}" if i+1<len(JOB_RANKS) else f"Jobs {min_wc}+"
        lines.append(f"{emoji} **{name}** — *{JOB_TITLES.get(name,name)}*\n  └ {r} | **{salary:,}** cr/job")
    e.description="\n".join(lines)
    e.set_footer(text="+2%/job within rank (max 1.5×) • Prestige multiplier applies")
    await ctx.send(embed=e)

@bot.command()
async def trade(ctx, member: discord.Member, *, args: str=""):
    if member.bot or member.id==ctx.author.id: await ctx.send("❌ Invalid target."); return
    uid=ctx.author.id; ensure_user(uid,str(ctx.author)); ensure_user(member.id,str(member))
    coin_ids=[]; credits_offer=0
    for part in args.strip().split():
        if part.lower().startswith("credits:"):
            try: credits_offer=int(part.split(":")[1])
            except: await ctx.send("❌ Use `credits:500`"); return
        elif part:
            try: coin_ids=[int(x.strip()) for x in part.split(",") if x.strip()]
            except: await ctx.send("❌ Invalid coin IDs."); return
    if not coin_ids and credits_offer==0: await ctx.send("❌ Specify coins and/or credits. E.g. `-trade @Bob 12,15 credits:500`"); return
    if coin_ids:
        conn=db(); cur=conn.cursor()
        try:
            phs=','.join(['%s']*len(coin_ids))
            cur.execute(f"SELECT id FROM coins WHERE id IN ({phs}) AND owner_id=%s",coin_ids+[uid])
            found={r['id'] for r in cur.fetchall()}
        finally: release(conn)
        inv=set(coin_ids)-found
        if inv: await ctx.send(f"❌ Coins `{inv}` not in your inventory."); return
    if credits_offer>0:
        u=get_user(uid)
        if not u or u['credits']<credits_offer: await ctx.send("❌ Insufficient credits."); return
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("INSERT INTO trades(initiator_id,receiver_id,coin_ids,credits_offer) VALUES(%s,%s,%s,%s) RETURNING id",
            (uid,member.id,",".join(str(x) for x in coin_ids),credits_offer))
        tid=cur.fetchone()['id']; conn.commit()
    except Exception as ex:
        conn.rollback(); await ctx.send("❌ Error creating trade."); return
    finally: release(conn)
    e=discord.Embed(title=f"🤝 Trade Offer #{tid}",color=0xFEE75C)
    e.description=f"**{ctx.author.display_name}** → **{member.display_name}**"
    lines=[]
    if coin_ids: lines.append(f"Coins: `{', '.join('#'+str(i) for i in coin_ids)}`")
    if credits_offer>0:
        net=credits_offer-int(credits_offer*TRADE_TAX_PCT)-int(credits_offer*ECONOMY_SINK_PCT)
        lines.append(f"Credits: **{credits_offer:,}** (receiver gets **{net:,}** after fees)")
    e.add_field(name="📤 Offer",value="\n".join(lines) or "None",inline=False)
    e.set_footer(text="Expires in 2 minutes")
    await ctx.send(f"{member.mention}",embed=e,view=TradeView(tid,uid,member.id))

@bot.command()
async def trades(ctx):
    uid=ctx.author.id
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT * FROM trades WHERE (initiator_id=%s OR receiver_id=%s) AND status='pending' ORDER BY created_at DESC LIMIT 10",(uid,uid))
        rows=cur.fetchall()
    finally: release(conn)
    if not rows: await ctx.send("📭 No pending trades."); return
    e=discord.Embed(title="📋 Pending Trades",color=0xFEE75C)
    for t in rows:
        role="Sender" if t['initiator_id']==uid else "Receiver"
        e.add_field(name=f"Trade #{t['id']} [{role}]",
            value=f"Coins: `{t['coin_ids'] or 'none'}` | Credits: {t['credits_offer']:,}\nCreated: {t['created_at'].strftime('%Y-%m-%d %H:%M')}",inline=False)
    await ctx.send(embed=e)

@bot.command()
async def auction(ctx, coin_id: int, start_price: int, hours: float=24.0):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    if start_price<=0: await ctx.send("❌ Start price must be > 0."); return
    if not 1<=hours<=168: await ctx.send("❌ Duration: 1–168 hours."); return
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT * FROM coins WHERE id=%s AND owner_id=%s",(coin_id,uid))
        c=cur.fetchone()
        if not c: await ctx.send(f"❌ Coin #{coin_id} not in inventory."); return
        cur.execute("SELECT id FROM auctions WHERE coin_id=%s AND status='active'",(coin_id,))
        if cur.fetchone(): await ctx.send(f"❌ Coin #{coin_id} already listed."); return
        ends_at=datetime.now(timezone.utc)+timedelta(hours=hours)
        cur.execute("INSERT INTO auctions(seller_id,coin_id,start_price,ends_at) VALUES(%s,%s,%s,%s) RETURNING id",(uid,coin_id,start_price,ends_at))
        aid=cur.fetchone()['id']; conn.commit()
    except: conn.rollback(); await ctx.send("❌ Error."); return
    finally: release(conn)
    mkt=get_market_price(c)
    e=discord.Embed(title="🏪 Listed!",color=0x57F287)
    e.add_field(name="🪙 Coin",   value=coin_name(c),      inline=True)
    e.add_field(name="💰 Start",  value=f"{start_price:,}",inline=True)
    e.add_field(name="📈 Market", value=f"${mkt:.4f}",      inline=True)
    e.add_field(name="Ends",      value=f"<t:{int(ends_at.timestamp())}:R>",inline=True)
    e.set_footer(text=f"Auction #{aid} • {int(MARKET_FEE_PCT*100)}% fee + {int(ECONOMY_SINK_PCT*100)}% sink")
    await ctx.send(embed=e)

@bot.command()
async def market(ctx, page: int=1):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    e=discord.Embed(title="🏪 Loading market...",color=0xEB459E)
    msg=await ctx.send(embed=e)
    await _send_market_view(msg, uid, page, "ends_asc", None)

@bot.command()
async def bid(ctx, auction_id: int):
    ensure_user(ctx.author.id,str(ctx.author))
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT * FROM auctions WHERE id=%s AND status='active'",(auction_id,))
        a=cur.fetchone()
    finally: release(conn)
    if not a: await ctx.send(f"❌ Auction #{auction_id} not found."); return
    min_bid=max(a['start_price'],(a['current_bid'] or 0)+1)
    await ctx.send(f"💰 Bidding on Auction **#{auction_id}** | Min: **{min_bid:,}**",view=AuctionView(auction_id))

@bot.command()
async def myauctions(ctx):
    uid=ctx.author.id
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT a.*,c.material,c.variant,c.serial,c.custom_name FROM auctions a JOIN coins c ON c.id=a.coin_id WHERE a.seller_id=%s AND a.status='active' ORDER BY a.ends_at ASC",(uid,))
        rows=cur.fetchall()
    finally: release(conn)
    if not rows: await ctx.send("📭 No active listings."); return
    e=discord.Embed(title="📋 Your Auctions",color=0xEB459E)
    for a in rows:
        tb=f"{a['current_bid']:,}" if a['current_bid'] else "No bids"
        name=a.get('custom_name') or f"{a['variant']} {a['material']} Coin"
        ends=a['ends_at']
        if ends.tzinfo is None: ends=ends.replace(tzinfo=timezone.utc)
        e.add_field(name=f"#{a['id']} — {name}",
            value=f"Start: {a['start_price']:,} | Top: {tb} | Ends: <t:{int(ends.timestamp())}:R>",inline=False)
    await ctx.send(embed=e)

@bot.command()
async def cancelauction(ctx, auction_id: int):
    uid=ctx.author.id
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT * FROM auctions WHERE id=%s AND seller_id=%s AND status='active'",(auction_id,uid))
        a=cur.fetchone()
        if not a: await ctx.send(f"❌ Auction #{auction_id} not found."); return
        if a['bidder_id'] and a['current_bid']: cur.execute("UPDATE users SET credits=credits+%s WHERE user_id=%s",(a['current_bid'],a['bidder_id'])); _cache_invalidate(a['bidder_id'])
        cur.execute("UPDATE coins SET owner_id=%s WHERE id=%s",(uid,a['coin_id']))
        cur.execute("UPDATE auctions SET status='cancelled' WHERE id=%s",(auction_id,))
        conn.commit()
    except: conn.rollback(); await ctx.send("❌ Error."); return
    finally: release(conn)
    await ctx.send(f"✅ Auction **#{auction_id}** cancelled. Coin returned.")

@bot.command(aliases=['lb'])
async def leaderboard(ctx):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    e=discord.Embed(title="🏆 Loading leaderboard...",color=0xFFD700)
    msg=await ctx.send(embed=e)
    await _send_leaderboard_view(msg, uid, "portfolio")

@bot.command()
async def profile(ctx, member: discord.Member=None):
    target=member or ctx.author; uid=target.id; ensure_user(uid,str(target))
    if member and member.id!=ctx.author.id:
        tu=get_user(uid)
        if not tu or not tu.get('inventory_public'): await ctx.send(f"🔒 **{target.display_name}'s** profile is private."); return
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT * FROM users WHERE user_id=%s",(uid,))
        u=cur.fetchone()
        cur.execute("SELECT value FROM coins WHERE owner_id=%s ORDER BY value DESC LIMIT 1",(uid,))
        best=cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(value),0) as t FROM coins WHERE owner_id=%s",(uid,))
        tv=float(cur.fetchone()['t'])
        cur.execute("SELECT COUNT(*) as c FROM auctions WHERE seller_id=%s AND status='sold'",(uid,))
        sales=cur.fetchone()['c']
    finally: release(conn)
    if not u: await ctx.send("❌ User not found."); return
    apply_bank_interest(uid); vb=get_user_bank(uid)
    pv=u.get('prestige') or 0; wc=u.get('work_count') or 0
    rn,_,re,_=get_job_rank(wc); jt=JOB_TITLES.get(rn,rn)
    e=discord.Embed(title=f"👤 {target.display_name}",color=0x5865F2)
    e.set_thumbnail(url=target.display_avatar.url)
    e.add_field(name="🎟️ Credits",    value=f"{u['credits']:,}",           inline=True)
    e.add_field(name="🏦 Bank",        value=f"{vb['balance']:,}",           inline=True)
    e.add_field(name="🪙 Coins",       value=str(u['total_coins']),          inline=True)
    e.add_field(name="⭐ Prestige",    value=f"{pv} (×{prestige_multiplier(pv):.1f})", inline=True)
    e.add_field(name="📈 Portfolio",   value=f"${tv:.4f}",                   inline=True)
    e.add_field(name="🏆 Best Coin",   value=f"${best['value']:.4f}" if best else "None", inline=True)
    e.add_field(name=f"{re} Job",      value=f"**{jt}** (#{wc} jobs)",       inline=True)
    e.add_field(name="💸 Sales",       value=str(sales),                     inline=True)
    e.add_field(name="🔥 Streak",      value=f"{u.get('daily_streak',0)}d",  inline=True)
    joined=u.get('joined_at'); priv="🔓 Public" if u.get('inventory_public') else "🔒 Private"
    e.set_footer(text=f"Joined {joined.strftime('%Y-%m-%d') if joined else '?'} • {priv}")
    await ctx.send(embed=e)

@bot.command()
async def prices(ctx, category: str="all"):
    cat=category.lower()
    lu=_market_prices.get("last_updated")
    if lu:
        nxt=lu+timedelta(minutes=20)-datetime.now(timezone.utc)
        sc=max(0,int(nxt.total_seconds())); mm,ss=divmod(sc,60)
        nxt_str=f"Next in **{mm}m {ss}s**"; last_str=lu.strftime("%H:%M UTC")
    else: nxt_str="Unknown"; last_str="Not set"

    if cat in ("material","materials","mat"):
        e=discord.Embed(title="📊 Material Prices",color=0xFFD700)
        lines=[]
        for mat,(lo,hi) in MATERIAL_PRICE_RANGES.items():
            cur_p=_market_prices["materials"].get(mat,0); mid=(lo+hi)/2
            arr="📈" if cur_p>mid else "📉"; ev=get_event_mult(mat); sd=get_supply_demand_mult(mat)
            tags=""
            if ev>1.0: tags+=" 🚀BOOM"
            elif ev<1.0: tags+=" 💥BUST"
            if sd<0.95: tags+=" 📦oversupply"
            elif sd>1.05: tags+=" 🔥scarce"
            lines.append(f"{arr} **{mat}**: `{cur_p:.4f}×`{tags}")
        e.description=f"Updated: **{last_str}** • {nxt_str}\n\n"+"\n".join(lines)
    elif cat in ("float","floats","flt"):
        e=discord.Embed(title="📊 Float Prices",color=0x5865F2)
        lines=[]
        for flt,(lo,hi) in FLOAT_PRICE_RANGES.items():
            cur_p=_market_prices["floats"].get(flt,0); mid=(lo+hi)/2; arr="📈" if cur_p>mid else "📉"
            lines.append(f"{arr} **{flt}**: `{cur_p:.4f}×`")
        e.description=f"Updated: **{last_str}** • {nxt_str}\n\n"+"\n".join(lines)
    elif cat in ("status","statuses","sta"):
        e=discord.Embed(title="📊 Status Prices",color=0xEB459E)
        lines=[]
        for sta,(lo,hi) in STATUS_PRICE_RANGES.items():
            cur_p=_market_prices["statuses"].get(sta,1.0); mid=(lo+hi)/2; arr="📈" if cur_p>mid else "📉"
            note=" *(30d bonus: 2×–30×)*" if sta=="Old" else ""
            lines.append(f"{arr} **{sta}**: `{cur_p:.4f}×`{note}")
        e.description=f"Updated: **{last_str}** • {nxt_str}\n\n"+"\n".join(lines)
    else:
        e=discord.Embed(title="📈 All Market Prices",color=0xEB459E)
        e.description=f"Updated: **{last_str}** • {nxt_str}"
        mat_l=[f"{'📈' if _market_prices['materials'].get(m,0)>(lo+hi)/2 else '📉'} **{m}**: `{_market_prices['materials'].get(m,0):.3f}×`{'🚀' if get_event_mult(m)>1.0 else ('💥' if get_event_mult(m)<1.0 else '')}" for m,(lo,hi) in MATERIAL_PRICE_RANGES.items()]
        flt_l=[f"{'📈' if _market_prices['floats'].get(f,0)>(lo+hi)/2 else '📉'} **{f}**: `{_market_prices['floats'].get(f,0):.3f}×`" for f,(lo,hi) in FLOAT_PRICE_RANGES.items()]
        sta_l=[f"{'📈' if _market_prices['statuses'].get(s,1.0)>(lo+hi)/2 else '📉'} **{s}**: `{_market_prices['statuses'].get(s,1.0):.3f}×`" for s,(lo,hi) in STATUS_PRICE_RANGES.items()]
        e.add_field(name="🪨 Materials",value="\n".join(mat_l),inline=True)
        e.add_field(name="🌊 Floats",   value="\n".join(flt_l),inline=True)
        e.add_field(name="🏷️ Statuses", value="\n".join(sta_l),inline=True)
        e.set_footer(text="Use -prices material / float / status for details")
    await ctx.send(embed=e)

@bot.command()
async def coinprice(ctx, coin_id: int):
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT * FROM coins WHERE id=%s",(coin_id,))
        c=cur.fetchone()
    finally: release(conn)
    if not c: await ctx.send(f"❌ Coin #{coin_id} not found."); return
    mkt=get_market_price(c); base=c['value']; diff=mkt-base; pct=(diff/base*100) if base>0 else 0
    tier=tier_emoji(mkt); rap=get_coin_rap(coin_id); ev=get_event_mult(c['material']); sd=get_supply_demand_mult(c['material'])
    e=discord.Embed(title=f"{tier} Live Price — {coin_name(c)} #{str(c['serial']).zfill(4)}",color=0xFFD700)
    e.add_field(name="📦 Stored", value=f"${base:.4f}",                              inline=True)
    e.add_field(name="📈 Market", value=f"**${mkt:.4f}**",                           inline=True)
    e.add_field(name="📊 Change", value=f"{'+' if diff>=0 else ''}{diff:.4f} ({pct:+.1f}%)", inline=True)
    e.add_field(name="💹 RAP",    value=f"**${rap:,.2f}**" if rap else "No trades",  inline=True)
    econ=[]
    if ev!=1.0: econ.append(f"{'🚀 BOOM' if ev>1.0 else '💥 BUST'} ×{ev}")
    if abs(sd-1.0)>0.01: econ.append(f"{'📦 Oversupply' if sd<1.0 else '🔥 Scarce'} ×{sd:.3f}")
    if econ: e.add_field(name="🌐 Economy",value="\n".join(econ),inline=False)
    await ctx.send(embed=e)

@bot.command()
async def economy(ctx):
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) as c FROM users"); tu=cur.fetchone()['c']
        cur.execute("SELECT COUNT(*) as c FROM coins"); tc=cur.fetchone()['c']
        cur.execute("SELECT COALESCE(SUM(value),0) as v FROM coins"); tv=float(cur.fetchone()['v'])
        cur.execute("SELECT COALESCE(SUM(credits),0) as c FROM users"); tw=int(cur.fetchone()['c'])
        cur.execute("SELECT COALESCE(SUM(balance),0) as b FROM user_bank"); tb=int(cur.fetchone()['b'])
        cur.execute("SELECT total FROM bank WHERE id=1"); tr=int(cur.fetchone()['total'])
        cur.execute("SELECT material,total_sold FROM material_sales_log ORDER BY total_sold DESC LIMIT 5"); top=cur.fetchall()
    except Exception as e:
        print(f"economy error: {e}"); await ctx.send("❌ Error."); return
    finally: release(conn)
    cc=get_dynamic_crate_cost(); inf=round((cc/CRATE_COST-1)*100,1)
    e=discord.Embed(title="🌐 Economy Overview",color=0x57F287)
    e.add_field(name="📊 Scale",value=f"Users: **{tu:,}**\nCoins: **{tc:,}**\nTotal portfolio: **${tv:,.2f}**\nCredits in system: **{tw+tb+tr:,}**",inline=False)
    e.add_field(name="💸 Credits",value=f"Wallets: **{tw:,}**\nBanks: **{tb:,}**\nTreasury: **{tr:,}**",inline=True)
    e.add_field(name="📦 Inflation",value=f"Crate: **{cc}** (+{inf}%)\nCap: **{MAX_CRATE_COST}**",inline=True)
    now=datetime.now(timezone.utc)
    evs=[(m,ev) for m,ev in _market_events.items() if now<ev["expires"]]
    if evs:
        e.add_field(name="⚡ Events",value="\n".join(f"{'🚀' if ev['type']=='boom' else '💥'} **{m}** ×{ev['mult']} ({int((ev['expires']-now).total_seconds()//60)}m left)" for m,ev in evs),inline=False)
    if top: e.add_field(name="🏆 Top Sold",value="\n".join(f"**{r['material']}**: {r['total_sold']:,}" for r in top),inline=False)
    await ctx.send(embed=e)

@bot.command()
async def marketevents(ctx):
    now=datetime.now(timezone.utc)
    active=[(m,ev) for m,ev in _market_events.items() if now<ev["expires"]]
    if not active: await ctx.send("📊 No active events."); return
    e=discord.Embed(title="⚡ Market Events",color=0xFFD700)
    for mat,ev in active:
        rem=ev["expires"]-now; m=int(rem.total_seconds()//60); s=int(rem.total_seconds()%60)
        icon="🚀" if ev["type"]=="boom" else "💥"
        e.add_field(name=f"{icon} {mat} — {ev['type'].upper()}",
            value=f"×**{ev['mult']}** | Price: `{_market_prices['materials'].get(mat,'?'):.4f}×` | {m}m {s}s left",inline=True)
    e.set_footer(text="Boom=sell • Bust=hold")
    await ctx.send(embed=e)

@bot.command()
async def bank(ctx):
    total=get_bank_total(); n=count_users(); share=total//n if n>0 else 0
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT * FROM daily_log ORDER BY paid_date DESC LIMIT 1"); last=cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(amount),0) as t FROM bank_log WHERE logged_at>NOW()-INTERVAL '24 hours' AND amount>0"); inc=cur.fetchone()['t']
    finally: release(conn)
    e=discord.Embed(title="🏦 Treasury",color=0x57F287)
    e.add_field(name="💰 Balance",    value=f"**{total:,}**",   inline=True)
    e.add_field(name="👥 Users",      value=str(n),              inline=True)
    e.add_field(name="📤 Share",      value=f"**{share:,}**/user",inline=True)
    e.add_field(name="📈 24h Inflow", value=f"{int(inc):,}",    inline=True)
    if last: e.add_field(name="📅 Last Payout",value=f"{last['paid_date']} — {last['amount']:,}/user",inline=True)
    e.set_footer(text="Funded by: crate fees • taxes • gambling • rob fines • prestige")
    await ctx.send(embed=e)

# ─── Black Market ──────────────────────────────────────────────────────────────
@bot.command()
async def blackmarket(ctx):
    if not isinstance(ctx.channel,discord.DMChannel):
        try: await ctx.message.delete()
        except: pass
        await ctx.send("⚠️ Black Market is **DM only**.",delete_after=8); return
    uid=ctx.author.id
    if not await has_black_market_role(ctx.author): await ctx.send("❌ Missing required role."); return
    bm=get_user_bm(uid)
    if not bm: await ctx.send("🌑 No active Black Market. It appears every 2h with 10% chance!"); return
    now=datetime.now(timezone.utc); tl=bm["expires"]-now; m=int(tl.total_seconds()//60); s=int(tl.total_seconds()%60)
    e=discord.Embed(title="🌑 YOUR BLACK MARKET",description=f"⏳ Closes in **{m}m {s}s**\nUse `-bmbuy <key>` to purchase.\n",color=0x2F3136)
    for key,item in BLACK_MARKET_ITEMS.items():
        stock=bm["stock"].get(key,0)
        if stock>0: e.add_field(name=f"{item['emoji']} {item['name']} — {item['price']:,} cr",value=f"{item['desc']}\nStock: **{stock}** | Key: `{key}`",inline=False)
        else: e.add_field(name=f"~~{item['emoji']} {item['name']}~~ — SOLD OUT",value=f"~~{item['desc']}~~",inline=False)
    e.set_footer(text="DM only • Extremely rare items")
    await ctx.send(embed=e)

@bot.command()
async def bmbuy(ctx, item_key: str=None):
    if not isinstance(ctx.channel,discord.DMChannel):
        try: await ctx.message.delete()
        except: pass
        await ctx.send("⚠️ DM only.",delete_after=8); return
    uid=ctx.author.id
    if not await has_black_market_role(ctx.author): await ctx.send("❌ Missing required role."); return
    if not item_key: await ctx.send("❌ Usage: `-bmbuy <item_key>`"); return
    bm=get_user_bm(uid)
    if not bm: await ctx.send("❌ No active Black Market."); return
    item_key=item_key.lower()
    if item_key not in BLACK_MARKET_ITEMS: await ctx.send("❌ Unknown item. See `-blackmarket`."); return
    if bm["stock"].get(item_key,0)<=0: await ctx.send("❌ Out of stock!"); return
    item=BLACK_MARKET_ITEMS[item_key]; ensure_user(uid,str(ctx.author))
    inv=get_user_items(uid)
    perm={"data_leak_generator":"has_dlg","suspicious_os":"has_sos","transfer_system":"has_transfer","ai_machine":"has_ai_machine"}
    if item_key in perm and inv.get(perm[item_key]): await ctx.send(f"❌ Already own **{item['name']}**."); return
    e=discord.Embed(title="🌑 Purchase",description=f"{item['emoji']} **{item['name']}**\n{item['desc']}\n\nPrice: **{item['price']:,} cr**",color=0x2F3136)
    await ctx.send(embed=e,view=BlackMarketBuyView(item_key,item['price'],uid))

@bot.command()
async def usetracker(ctx, member: discord.Member=None):
    if not isinstance(ctx.channel,discord.DMChannel):
        try: await ctx.message.delete()
        except: pass
        await ctx.send("⚠️ DM only.",delete_after=8); return
    if not await has_black_market_role(ctx.author): await ctx.send("❌ Missing required role."); return
    if not member: await ctx.send("❌ Usage: `-usetracker @user`"); return
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT id FROM black_market_log WHERE user_id=%s AND item_key='identity_tracker' ORDER BY bought_at DESC LIMIT 1",(uid,))
        tracker=cur.fetchone()
        if not tracker: await ctx.send("❌ No **Identity Tracker** in inventory."); return
        ensure_user(member.id,str(member))
        tvb=get_user_bank(member.id); bid=tvb.get('bank_id') or "No bank ID."
        cur.execute("DELETE FROM black_market_log WHERE id=%s",(tracker['id'],)); conn.commit()
    except Exception as e:
        conn.rollback(); await ctx.send("❌ Error."); return
    finally: release(conn)
    await ctx.send(f"🕵️ **Tracker Result**\nTarget: **{member}**\n```\n{bid}\n```\n⚠️ One-time use consumed.")

# ─── Hacking ───────────────────────────────────────────────────────────────────
@bot.command()
async def hack(ctx, bank_id: str=None):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    if not isinstance(ctx.channel,discord.DMChannel):
        try: await ctx.message.delete()
        except: pass
        await ctx.send("⚠️ Hacking is **DM only**.",delete_after=8); return
    if not await has_black_market_role(ctx.author): await ctx.send("❌ Missing required role."); return
    if not bank_id: await ctx.send("❌ Usage: `-hack <bank_id>`"); return
    items=get_user_items(uid)
    if not items.get('has_sos'): await ctx.send("❌ Need **Suspicious Operating System**."); return
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT user_id,balance FROM user_bank WHERE bank_id=%s",(bank_id,)); tb=cur.fetchone()
        if not tb: await ctx.send("❌ Bank ID not found."); return
        if tb['user_id']==uid: await ctx.send("❌ Can't hack yourself."); return
        cur.execute("SELECT id FROM hack_transfers WHERE hacker_id=%s AND done=FALSE",(uid,)); pending=cur.fetchone()
        if pending: await ctx.send("❌ Pending transfer in progress. Wait for it."); return
    finally: release(conn)
    fc=get_hack_progress(uid,bank_id)
    bonus=fc*0.01 if items.get('has_dlg') else 0.0
    chance=min(HACK_BASE_CHANCE+bonus,HACK_MAX_CHANCE)
    hv=HackChallengeView(uid,bank_id,tb['user_id'],chance)
    e=discord.Embed(title="💻 Hacking Terminal",
        description=(f"🎯 Target: `{bank_id}`\n💰 Vault: **{tb['balance']:,}**\n📊 Chance: **{chance*100:.1f}%**\n\n"
                     f"**Stage 1/3:** {hv.challenge}\n\nSolve all 3 to maximize success!"),color=0x00FF41)
    sv=HackStageView(hv,None); msg=await ctx.send(embed=e,view=sv); sv.dm_msg=msg

@bot.command()
async def hackinventory(ctx):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author)); items=get_user_items(uid)
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT * FROM data_cards WHERE owner_id=%s ORDER BY created_at DESC LIMIT 10",(uid,)); cards=cur.fetchall()
        cur.execute("SELECT * FROM hack_progress WHERE hacker_id=%s ORDER BY fail_count DESC",(uid,)); prog=cur.fetchall()
    finally: release(conn)
    tick=lambda h: "✅" if h else "❌"
    e=discord.Embed(title="🔓 Hack Inventory",color=0x00FF41)
    e.add_field(name="💻 Suspicious OS",  value=tick(items.get('has_sos')),     inline=True)
    e.add_field(name="💾 Data Leak Gen",  value=tick(items.get('has_dlg')),     inline=True)
    e.add_field(name="📡 Transfer System",value=tick(items.get('has_transfer')),inline=True)
    if cards:
        e.add_field(name="🃏 Data Cards",value="\n".join(f"`{c['target_bank_id'][:20]}...` — {c['fail_count_snapshot']} fail(s)" for c in cards),inline=False)
    if prog:
        e.add_field(name="📊 Progress",value="\n".join(f"`{p['target_bank_id'][:25]}...` — {p['fail_count']} fail(s) → {min(HACK_BASE_CHANCE*100+p['fail_count'],HACK_MAX_CHANCE*100):.1f}%" for p in prog),inline=False)
    e.set_footer(text="-hack <bank_id> in DMs • Transfer delay: 2 hours")
    await ctx.send(embed=e)

@bot.command()
async def hacktransfers(ctx):
    uid=ctx.author.id; ensure_user(uid,str(ctx.author))
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("SELECT * FROM hack_transfers WHERE hacker_id=%s ORDER BY completes_at ASC",(uid,)); rows=cur.fetchall()
    finally: release(conn)
    if not rows: await ctx.send("📡 No hack transfers."); return
    e=discord.Embed(title="📡 Hack Transfers",color=0x00FF41)
    for r in rows:
        comp=r['completes_at']
        if comp.tzinfo is None: comp=comp.replace(tzinfo=timezone.utc)
        status="✅ Done" if r['done'] else f"⏳ <t:{int(comp.timestamp())}:R>"
        e.add_field(name=f"Transfer #{r['id']}",value=f"**{r['amount']:,} credits** | {status}",inline=False)
    await ctx.send(embed=e)

# ─── Admin ─────────────────────────────────────────────────────────────────────
@bot.command(aliases=['removecredits','deduct'])
async def rmcredits(ctx, member: discord.Member, amount: int):
    if ctx.author.id!=ADMIN_ID: await ctx.send("❌ No permission."); return
    if amount<=0: await ctx.send("❌ Positive only."); return
    ensure_user(member.id,str(member))
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("UPDATE users SET credits=GREATEST(0,credits-%s) WHERE user_id=%s",(amount,member.id))
        cur.execute("INSERT INTO credit_log(user_id,amount,reason) VALUES(%s,%s,'admin_deduct')",(member.id,-amount))
        conn.commit(); _cache_invalidate(member.id)
    except: conn.rollback(); await ctx.send("❌ Error."); return
    finally: release(conn)
    u=get_user(member.id)
    await ctx.send(embed=discord.Embed(title="🛠️ Credits Removed",color=discord.Color.orange(),
        description=f"**{member}** — Removed **{amount:,}** | New balance: **{u['credits']:,}**"))

@bot.command()
async def addcredits(ctx, member: discord.Member, amount: int):
    if ctx.author.id!=ADMIN_ID: await ctx.send("❌ No permission."); return
    ensure_user(member.id,str(member))
    add_credits(member.id, amount, "admin_add")
    u=get_user(member.id)
    await ctx.send(embed=discord.Embed(title="🛠️ Credits Added",color=discord.Color.green(),
        description=f"**{member}** — Added **{amount:,}** | New balance: **{u['credits']:,}**"))

@bot.command()
async def givecoin(ctx, member: discord.Member):
    if ctx.author.id!=ADMIN_ID: await ctx.send("❌ No permission."); return
    ensure_user(member.id,str(member))
    c=generate_coin()
    conn=db(); cur=conn.cursor()
    try:
        cur.execute("""INSERT INTO coins(owner_id,material,variant,status,float,serial,
            base_value,mat_mult,var_mult,sta_mult,flt_mult,ser_mult,total_mult,value)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (member.id,c['material'],c['variant'],c['status'],c['float'],c['serial'],
             c['base_value'],c['mat_mult'],c['var_mult'],c['sta_mult'],c['flt_mult'],
             c['ser_mult'],c['total_mult'],c['value']))
        sync_coin_count(member.id,cur); conn.commit(); _cache_invalidate(member.id)
    except: conn.rollback(); await ctx.send("❌ Error."); return
    finally: release(conn)
    mkt=get_market_price(c)
    await ctx.send(f"✅ Gave **{member}** a **{c['variant']} {c['material']} Coin** (Market: ${mkt:.4f})")

@bot.command()
async def setannouncechannel(ctx):
    if ctx.author.id!=ADMIN_ID: await ctx.send("❌ No permission."); return
    global _announce_channel_id; _announce_channel_id=ctx.channel.id
    await ctx.send(f"✅ Announcements → **#{ctx.channel.name}**")

@bot.command()
async def forcemarketupdate(ctx):
    if ctx.author.id!=ADMIN_ID: await ctx.send("❌ No permission."); return
    event=generate_market_prices()
    if event: ev_type,mat,mult=event; await ctx.send(f"✅ Market updated! **{ev_type.upper()}** on **{mat}** ×{mult}")
    else: await ctx.send("✅ Market updated. No event.")

@bot.command()
async def forceblackmarket(ctx, member: discord.Member=None):
    if ctx.author.id!=ADMIN_ID: await ctx.send("❌ No permission."); return
    target=member or ctx.author; bm=spawn_user_bm(target.id)
    await ctx.send(f"✅ Black Market spawned for **{target}** for {BLACK_MARKET_DURATION_MIN} minutes.")

# ─── Run ───────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
