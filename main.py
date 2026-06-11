import os, random, asyncio, time, re
from datetime import datetime, timezone, timedelta
from discord.ext import commands, tasks
from flask import Flask
from threading import Thread
import discord, psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

# ─── Flask Keep-Alive ─────────────────────────────────────────────────────────
app = Flask('')
@app.route('/')
def home(): return "Bot is alive!"
Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# ─── DB Pool ──────────────────────────────────────────────────────────────────
DB_URL = os.getenv('DATABASE_URL') or exit("ERROR: DATABASE_URL missing!")
_pool = None

def get_pool():
    global _pool
    if _pool is None or _pool.closed:
        _pool = ThreadedConnectionPool(2, 10, dsn=DB_URL, cursor_factory=RealDictCursor,
                                       sslmode='require', connect_timeout=10)
    return _pool

class _Conn:
    def __enter__(self):
        self.conn = get_pool().getconn(); return self.conn
    def __exit__(self, exc_type, *_):
        if exc_type: self.conn.rollback()
        get_pool().putconn(self.conn)

def get_conn(): return _Conn()

# ─── Prize Cache ──────────────────────────────────────────────────────────────
_prize_cache, _prize_cache_ts = [], 0.0
PRIZE_CACHE_TTL = 60

def get_prizes_cached():
    global _prize_cache, _prize_cache_ts
    if time.time() - _prize_cache_ts > PRIZE_CACHE_TTL:
        _prize_cache = db_load_prizes(); _prize_cache_ts = time.time()
    return _prize_cache

def invalidate_prize_cache():
    global _prize_cache_ts; _prize_cache_ts = 0.0

# ─── DB Init ──────────────────────────────────────────────────────────────────
def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS prizes (
                    id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, image TEXT,
                    chance BIGINT NOT NULL, roll_message TEXT NOT NULL,
                    description TEXT, created_at TIMESTAMPTZ DEFAULT NOW());
                CREATE TABLE IF NOT EXISTS server_collection (
                    id SERIAL PRIMARY KEY, prize_name TEXT UNIQUE NOT NULL,
                    discovered BOOLEAN DEFAULT FALSE, first_user_id BIGINT,
                    first_user TEXT, first_at TIMESTAMPTZ, total_found INT DEFAULT 0);
                CREATE TABLE IF NOT EXISTS inventory (
                    id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, prize_name TEXT NOT NULL,
                    quantity INT DEFAULT 1, first_found_at TIMESTAMPTZ DEFAULT NOW(),
                    last_found_at TIMESTAMPTZ DEFAULT NOW(), UNIQUE(user_id, prize_name));
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id BIGINT PRIMARY KEY, bio TEXT, favorite_prize TEXT,
                    showcase_prize TEXT, equipped_prize TEXT,
                    total_messages BIGINT DEFAULT 0, created_at TIMESTAMPTZ DEFAULT NOW());

                CREATE TABLE IF NOT EXISTS wallets (
                    user_id BIGINT PRIMARY KEY,
                    balance   BIGINT DEFAULT 0,
                    bank      BIGINT DEFAULT 0,
                    streak    INT    DEFAULT 0,
                    last_msg_at TIMESTAMPTZ,
                    last_bank_interest_at TIMESTAMPTZ DEFAULT NOW());

                CREATE TABLE IF NOT EXISTS item_effects (
                    user_id      BIGINT NOT NULL,
                    effect_type  TEXT   NOT NULL,
                    value        BIGINT DEFAULT 0,
                    remaining    BIGINT DEFAULT 0,
                    expires_at   TIMESTAMPTZ,
                    PRIMARY KEY (user_id, effect_type));

                CREATE TABLE IF NOT EXISTS item_inventory (
                    user_id    BIGINT NOT NULL,
                    item_id    TEXT   NOT NULL,
                    quantity   INT    DEFAULT 0,
                    PRIMARY KEY (user_id, item_id));

                CREATE TABLE IF NOT EXISTS daily_store (
                    id          SERIAL PRIMARY KEY,
                    slot        INT    UNIQUE NOT NULL,
                    item_id     TEXT   NOT NULL,
                    stock       INT    NOT NULL,
                    price       INT    NOT NULL,
                    rotated_at  TIMESTAMPTZ DEFAULT NOW());

                CREATE TABLE IF NOT EXISTS spam_guard (
                    user_id       BIGINT PRIMARY KEY,
                    last_msg_hash TEXT,
                    repeat_count  INT DEFAULT 0,
                    muted_until   TIMESTAMPTZ);

                CREATE TABLE IF NOT EXISTS casino_stats (
                    user_id     BIGINT PRIMARY KEY,
                    total_bet   BIGINT DEFAULT 0,
                    total_won   BIGINT DEFAULT 0,
                    total_lost  BIGINT DEFAULT 0,
                    games_played INT DEFAULT 0,
                    biggest_win  BIGINT DEFAULT 0,
                    biggest_loss BIGINT DEFAULT 0);

                CREATE TABLE IF NOT EXISTS custom_replies (
                    id          SERIAL PRIMARY KEY,
                    trigger_uid BIGINT,
                    trigger_kw  TEXT,
                    reply       TEXT NOT NULL,
                    match_type  TEXT DEFAULT 'contains',
                    created_at  TIMESTAMPTZ DEFAULT NOW());
            """)
            for sql in [
                "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS last_found_at TIMESTAMPTZ DEFAULT NOW();",
                "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS equipped_prize TEXT;",
            ]: cur.execute(sql)
        conn.commit()
    print("✅ Database tables ready")

# ─── Item Catalog ─────────────────────────────────────────────────────────────
ITEMS = {
    "karma_potion":   {"name":"Karma Potion",   "tier":"common",    "price":140,  "effect":"luck_bonus",  "value":2_000,   "duration":1,    "desc":"+2,000 luck for your next message"},
    "blessed_potion": {"name":"Blessed Potion", "tier":"uncommon",  "price":430,  "effect":"luck_bonus",  "value":10_000,  "duration":1,    "desc":"+10,000 luck for your next message"},
    "godsend_potion": {"name":"Godsend Potion", "tier":"epic",      "price":780,  "effect":"luck_bonus",  "value":70_000,  "duration":1,    "desc":"+70,000 luck for your next message"},
    "divine_potion":  {"name":"Divine Potion",  "tier":"legendary", "price":1200, "effect":"luck_bonus",  "value":220_000, "duration":1,    "desc":"+220,000 luck for your next message"},
    "basic_stack":    {"name":"Basic Stack Syndrome",    "tier":"common",   "price":80,  "effect":"stack_mult", "value":2, "duration":100,   "desc":"×2 rolls per message for 100 messages"},
    "adv_stack":      {"name":"Advanced Stack Syndrome", "tier":"uncommon", "price":380, "effect":"stack_mult", "value":3, "duration":300,   "desc":"×3 rolls per message for 300 messages"},
    "master_stack":   {"name":"Master Stack Syndrome",   "tier":"epic",     "price":620, "effect":"stack_mult", "value":5, "duration":1000,  "desc":"×5 rolls per message for 1,000 messages"},
    "basic_skip":     {"name":"Basic Skip",   "tier":"uncommon",  "price":450,  "effect":"skip_msgs",  "value":500,    "duration":0,    "desc":"Skip 500 messages (counts toward streaks)"},
    "adv_skip":       {"name":"Advanced Skip","tier":"legendary", "price":970,  "effect":"skip_msgs",  "value":5000,   "duration":0,    "desc":"Skip 5,000 messages"},
    "master_skip":    {"name":"Master Skip",  "tier":"secret",    "price":5000, "effect":"skip_msgs",  "value":15000,  "duration":0,    "desc":"Skip 15,000 messages"},
}

ITEM_TIER_COLOR  = {"common":0x8B9099,"uncommon":0x2ECC71,"epic":0xC97FD4,"legendary":0xFF6B6B,"secret":0xF1C40F}
ITEM_TIER_EMOJI  = {"common":"⚪","uncommon":"🟢","epic":"💜","legendary":"💎","secret":"🌟"}
ITEM_TIER_BADGE  = {"common":"· COMMON","uncommon":"◉ UNCOMMON","epic":"◇ EPIC","legendary":"◆ LEGENDARY","secret":"✦ SECRET"}

DAILY_TIER_WEIGHTS = {"common":45,"uncommon":35,"epic":15,"legendary":4,"secret":1}
DAILY_SLOT_COUNT   = 5
DAILY_STOCK        = {"common":10,"uncommon":6,"epic":3,"legendary":2,"secret":1}

GENERAL_CHANNEL_ID      = 900029761638793236
ANNOUNCEMENT_CHANNEL_ID = 900015775069401128

# ─── Casino Config ────────────────────────────────────────────────────────────
MIN_BET      = 10
MAX_BET      = 500_000
CASINO_COLOR = 0xF1C40F

# Slot machine symbols: (emoji, weight, multiplier)
SLOT_SYMBOLS = [
    ("🍒", 35, 2),
    ("🍋", 25, 3),
    ("🍊", 18, 4),
    ("🍇", 12, 5),
    ("💎", 6,  10),
    ("7️⃣", 3,  20),
    ("🌑", 1,  100),
]

# ─── DB: Casino Stats ─────────────────────────────────────────────────────────
def db_ensure_casino(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO casino_stats(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (user_id,))
        conn.commit()

def db_get_casino_stats(user_id):
    db_ensure_casino(user_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM casino_stats WHERE user_id=%s", (user_id,))
            return cur.fetchone()

def db_record_casino(user_id, bet, net):
    """net positive = won, net negative = lost."""
    db_ensure_casino(user_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            if net >= 0:
                cur.execute("""UPDATE casino_stats SET
                    total_bet=total_bet+%s, total_won=total_won+%s,
                    games_played=games_played+1,
                    biggest_win=GREATEST(biggest_win,%s)
                    WHERE user_id=%s""", (bet, net, net, user_id))
            else:
                loss = abs(net)
                cur.execute("""UPDATE casino_stats SET
                    total_bet=total_bet+%s, total_lost=total_lost+%s,
                    games_played=games_played+1,
                    biggest_loss=GREATEST(biggest_loss,%s)
                    WHERE user_id=%s""", (bet, loss, loss, user_id))
        conn.commit()

def db_casino_leaderboard():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT user_id, total_won, total_lost, biggest_win, games_played,
                (total_won - total_lost) AS net
                FROM casino_stats ORDER BY net DESC LIMIT 10""")
            return cur.fetchall()

# ─── DB: Custom Replies ───────────────────────────────────────────────────────
def db_get_custom_replies():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM custom_replies ORDER BY id")
            return cur.fetchall()

def db_add_custom_reply(trigger_uid, trigger_kw, reply, match_type="contains"):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO custom_replies(trigger_uid,trigger_kw,reply,match_type) VALUES(%s,%s,%s,%s)",
                        (trigger_uid, trigger_kw, reply, match_type))
        conn.commit()

def db_delete_custom_reply(reply_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM custom_replies WHERE id=%s", (reply_id,))
        conn.commit()

def db_check_custom_reply(user_id, content):
    """Returns a matching reply text or None."""
    content_lower = content.lower()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM custom_replies")
            rows = cur.fetchall()
    for row in rows:
        # User trigger
        if row["trigger_uid"] and row["trigger_uid"] != user_id:
            continue
        kw = (row["trigger_kw"] or "").lower()
        mt = row["match_type"]
        if kw:
            if mt == "exact" and content_lower.strip() != kw:
                continue
            elif mt == "startswith" and not content_lower.startswith(kw):
                continue
            elif mt == "contains" and kw not in content_lower:
                continue
        return row["reply"]
    return None

# ─── DB: Wallets & Credits ────────────────────────────────────────────────────
def db_ensure_wallet(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO wallets(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (user_id,))
        conn.commit()

def db_get_wallet(user_id):
    db_ensure_wallet(user_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM wallets WHERE user_id=%s", (user_id,))
            return cur.fetchone()

def db_add_credits(user_id, amount):
    db_ensure_wallet(user_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE wallets SET balance=balance+%s WHERE user_id=%s", (amount, user_id))
        conn.commit()

def db_deduct_credits(user_id, amount):
    """Returns True if successful."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM wallets WHERE user_id=%s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if not row or row["balance"] < amount: return False
            cur.execute("UPDATE wallets SET balance=balance-%s WHERE user_id=%s", (amount, user_id))
        conn.commit()
    return True

def db_transfer_to_bank(user_id, amount):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM wallets WHERE user_id=%s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if not row or row["balance"] < amount: return False, "Insufficient wallet balance."
            cur.execute("UPDATE wallets SET balance=balance-%s, bank=bank+%s WHERE user_id=%s",
                        (amount, amount, user_id))
        conn.commit()
    return True, None

def db_transfer_from_bank(user_id, amount):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT bank FROM wallets WHERE user_id=%s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if not row or row["bank"] < amount: return False, "Insufficient bank balance."
            cur.execute("UPDATE wallets SET bank=bank-%s, balance=balance+%s WHERE user_id=%s",
                        (amount, amount, user_id))
        conn.commit()
    return True, None

def db_apply_bank_interest(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT bank FROM wallets WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            if row and row["bank"] > 0:
                interest = max(1, int(row["bank"] * 0.01))
                cur.execute("UPDATE wallets SET bank=bank+%s, last_bank_interest_at=NOW() WHERE user_id=%s",
                            (interest, user_id))
        conn.commit()

def db_leaderboard_credits():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, balance+bank AS total, balance, bank FROM wallets ORDER BY total DESC LIMIT 10")
            return cur.fetchall()

# ─── DB: Item Effects ─────────────────────────────────────────────────────────
def db_get_effects(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM item_effects WHERE user_id=%s", (user_id,))
            return {r["effect_type"]: r for r in cur.fetchall()}

def db_apply_effect(user_id, effect_type, value, remaining):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO item_effects(user_id,effect_type,value,remaining)
                VALUES(%s,%s,%s,%s) ON CONFLICT(user_id,effect_type)
                DO UPDATE SET value=%s, remaining=item_effects.remaining+%s""",
                (user_id, effect_type, value, remaining, value, remaining))
        conn.commit()

def db_tick_effect(user_id, effect_type, decrement=1):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value,remaining FROM item_effects WHERE user_id=%s AND effect_type=%s",
                        (user_id, effect_type))
            row = cur.fetchone()
            if not row or row["remaining"] <= 0:
                cur.execute("DELETE FROM item_effects WHERE user_id=%s AND effect_type=%s", (user_id, effect_type))
                conn.commit(); return 0, 0
            new_rem = row["remaining"] - decrement
            if new_rem <= 0:
                cur.execute("DELETE FROM item_effects WHERE user_id=%s AND effect_type=%s", (user_id, effect_type))
            else:
                cur.execute("UPDATE item_effects SET remaining=%s WHERE user_id=%s AND effect_type=%s",
                            (new_rem, user_id, effect_type))
            conn.commit(); return row["value"], max(0, new_rem)

# ─── DB: Item Inventory ───────────────────────────────────────────────────────
def db_get_item_inventory(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM item_inventory WHERE user_id=%s AND quantity>0", (user_id,))
            return cur.fetchall()

def db_add_item(user_id, item_id, qty=1):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO item_inventory(user_id,item_id,quantity) VALUES(%s,%s,%s)
                ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=item_inventory.quantity+%s""",
                (user_id, item_id, qty, qty))
        conn.commit()

def db_use_item(user_id, item_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT quantity FROM item_inventory WHERE user_id=%s AND item_id=%s FOR UPDATE",
                        (user_id, item_id))
            row = cur.fetchone()
            if not row or row["quantity"] < 1: return False
            cur.execute("UPDATE item_inventory SET quantity=quantity-1 WHERE user_id=%s AND item_id=%s",
                        (user_id, item_id))
        conn.commit(); return True

# ─── DB: Daily Store ──────────────────────────────────────────────────────────
def db_get_daily_store():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM daily_store ORDER BY slot")
            return cur.fetchall()

def db_rotate_store():
    tiers = list(DAILY_TIER_WEIGHTS.keys())
    weights = [DAILY_TIER_WEIGHTS[t] for t in tiers]
    chosen_items = []
    pool = [(iid, item) for iid, item in ITEMS.items()]
    for _ in range(DAILY_SLOT_COUNT):
        available = [(iid, item) for iid, item in pool if iid not in [x[0] for x in chosen_items]]
        if not available: break
        chosen_tiers = random.choices(tiers, weights=weights, k=len(available))
        tier_pool = [(iid, item) for (iid, item), t in zip(available, chosen_tiers) if item["tier"] == t]
        if not tier_pool: tier_pool = available
        chosen_items.append(random.choice(tier_pool))
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM daily_store")
            for slot, (iid, item) in enumerate(chosen_items):
                stock = DAILY_STOCK.get(item["tier"], 3)
                cur.execute("INSERT INTO daily_store(slot,item_id,stock,price,rotated_at) VALUES(%s,%s,%s,%s,%s)",
                            (slot, iid, stock, item["price"], now))
        conn.commit()
    return db_get_daily_store()

def db_buy_store_item(slot, user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM daily_store WHERE slot=%s FOR UPDATE", (slot,))
            row = cur.fetchone()
            if not row: raise ValueError("Invalid slot.")
            if row["stock"] < 1: raise ValueError("Out of stock!")
            cur.execute("SELECT balance FROM wallets WHERE user_id=%s FOR UPDATE", (user_id,))
            wallet = cur.fetchone()
            if not wallet or wallet["balance"] < row["price"]:
                raise ValueError(f"Not enough credits. Need **{row['price']:,}** ₡, have **{(wallet['balance'] if wallet else 0):,}** ₡.")
            cur.execute("UPDATE daily_store SET stock=stock-1 WHERE slot=%s", (slot,))
            cur.execute("UPDATE wallets SET balance=balance-%s WHERE user_id=%s", (row["price"], user_id))
        conn.commit()
    return row["item_id"], row["price"]

# ─── DB: Spam Guard ───────────────────────────────────────────────────────────
def db_check_spam(user_id, content) -> bool:
    normalized = re.sub(r'\s+', ' ', content.strip().lower())
    msg_hash = normalized[:120]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM spam_guard WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            now = datetime.now(timezone.utc)
            if row and row["muted_until"] and row["muted_until"] > now:
                return True
            is_spam = False
            repeat_count = 0
            if row:
                if row["last_msg_hash"] == msg_hash:
                    repeat_count = row["repeat_count"] + 1
                    if repeat_count >= 4: is_spam = True
                else:
                    repeat_count = 0
            if len(normalized) < 2 or normalized in {".", ",", "!", "?", "-", "a", "i"}:
                is_spam = True
            muted_until = None
            if is_spam and repeat_count >= 4:
                muted_until = now + timedelta(minutes=2)
            cur.execute("""INSERT INTO spam_guard(user_id,last_msg_hash,repeat_count,muted_until)
                VALUES(%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE
                SET last_msg_hash=%s, repeat_count=%s, muted_until=%s""",
                (user_id, msg_hash, repeat_count, muted_until,
                 msg_hash, repeat_count, muted_until))
        conn.commit()
    return is_spam

# ─── Credit Earning ───────────────────────────────────────────────────────────
def compute_credit_award(total_messages: int) -> int:
    credits = 1
    if   total_messages % 100_000 == 0: credits *= 10
    elif total_messages %  50_000 == 0: credits *= 5
    elif total_messages %  25_000 == 0: credits *= 4
    elif total_messages %  10_000 == 0: credits *= 3
    elif total_messages %   1_000 == 0: credits *= 2
    return credits

# ─── Existing DB Helpers ──────────────────────────────────────────────────────
def db_load_prizes():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM prizes ORDER BY chance DESC"); return cur.fetchall()

def db_get_prize(name):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM prizes WHERE name=%s", (name,)); return cur.fetchone()

def db_upsert_prize(data):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM prizes WHERE name=%s", (data['name'],))
            if cur.fetchone():
                cur.execute("UPDATE prizes SET image=%s,chance=%s,roll_message=%s,description=%s WHERE name=%s",
                    (data['image'], data['chance'], data['roll_message'], data['description'], data['name']))
                action = "Updated"
            else:
                cur.execute("INSERT INTO prizes (name,image,chance,roll_message,description) VALUES (%s,%s,%s,%s,%s)",
                    (data['name'], data['image'], data['chance'], data['roll_message'], data['description']))
                cur.execute("INSERT INTO server_collection (prize_name,discovered,total_found) VALUES (%s,FALSE,0) ON CONFLICT DO NOTHING",
                    (data['name'],))
                action = "Created"
        conn.commit()
    invalidate_prize_cache(); return action

def db_delete_prize(name):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM prizes WHERE name=%s", (name,))
            cur.execute("DELETE FROM server_collection WHERE prize_name=%s", (name,))
        conn.commit()
    invalidate_prize_cache()

def db_get_collection():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM server_collection ORDER BY prize_name"); return cur.fetchall()

def db_get_disc(prize_name):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM server_collection WHERE prize_name=%s", (prize_name,)); return cur.fetchone()

def db_record_roll(prize_name, user_id, user_name, now):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT discovered FROM server_collection WHERE prize_name=%s", (prize_name,))
            disc = cur.fetchone()
            if disc and not disc['discovered']:
                cur.execute("UPDATE server_collection SET discovered=TRUE,first_user_id=%s,first_user=%s,first_at=%s,total_found=total_found+1 WHERE prize_name=%s",
                    (user_id, user_name, now, prize_name))
            else:
                cur.execute("UPDATE server_collection SET total_found=total_found+1 WHERE prize_name=%s", (prize_name,))
            cur.execute("""INSERT INTO inventory (user_id,prize_name,quantity,first_found_at,last_found_at) VALUES (%s,%s,1,%s,%s)
                ON CONFLICT (user_id,prize_name) DO UPDATE SET quantity=inventory.quantity+1,last_found_at=%s""",
                (user_id, prize_name, now, now, now))
            cur.execute("INSERT INTO user_profiles (user_id,total_messages) VALUES (%s,1) ON CONFLICT (user_id) DO UPDATE SET total_messages=user_profiles.total_messages+1",
                (user_id,))
        conn.commit()

def db_increment_messages(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO user_profiles (user_id,total_messages) VALUES (%s,1) ON CONFLICT (user_id) DO UPDATE SET total_messages=user_profiles.total_messages+1",
                (user_id,))
        conn.commit()

def db_get_inventory_sorted(user_id, sort="rarity"):
    order = INV_SORT_OPTIONS.get(sort, INV_SORT_OPTIONS["rarity"])[1]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT i.*,p.chance FROM inventory i LEFT JOIN prizes p ON i.prize_name=p.name WHERE i.user_id=%s ORDER BY {order}",
                (user_id,)); return cur.fetchall()

def db_search_inventory(user_id, query):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT i.*,p.chance FROM inventory i LEFT JOIN prizes p ON i.prize_name=p.name WHERE i.user_id=%s AND i.prize_name ILIKE %s ORDER BY p.chance DESC NULLS LAST",
                (user_id, f'%{query}%')); return cur.fetchall()

def db_leaderboard_collected_stats():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT i.user_id, SUM(p.chance*i.quantity) AS collected_score, SUM(i.quantity) AS total
                FROM inventory i JOIN prizes p ON i.prize_name=p.name
                GROUP BY i.user_id ORDER BY collected_score DESC LIMIT 10"""); return cur.fetchall()

def db_leaderboard_rarest_finds():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.user_id, MAX(p.chance) AS rarest_chance,
                       p2.name AS rarest_prize_name, SUM(i.quantity) AS total
                FROM inventory i JOIN prizes p ON i.prize_name=p.name
                JOIN prizes p2 ON p2.chance=(SELECT MAX(p3.chance) FROM inventory i2
                    JOIN prizes p3 ON i2.prize_name=p3.name WHERE i2.user_id=i.user_id)
                AND p2.name IN (SELECT i3.prize_name FROM inventory i3
                    JOIN prizes p4 ON i3.prize_name=p4.name
                    WHERE i3.user_id=i.user_id AND p4.chance=(
                        SELECT MAX(p5.chance) FROM inventory i4
                        JOIN prizes p5 ON i4.prize_name=p5.name WHERE i4.user_id=i.user_id) LIMIT 1)
                GROUP BY i.user_id, p2.name ORDER BY rarest_chance DESC LIMIT 10
            """); return cur.fetchall()

def db_leaderboard_messages():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id,total_messages FROM user_profiles ORDER BY total_messages DESC LIMIT 10")
            return cur.fetchall()

def db_rarest():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT p.*,s.first_user,s.first_at,s.total_found FROM prizes p
                JOIN server_collection s ON p.name=s.prize_name
                WHERE s.discovered=TRUE ORDER BY p.chance DESC LIMIT 8"""); return cur.fetchall()

def db_stats():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT
                (SELECT COUNT(*) FROM prizes) AS total_prizes,
                (SELECT COUNT(*) FROM server_collection WHERE discovered=TRUE) AS discovered,
                (SELECT COUNT(*) FROM server_collection WHERE discovered=FALSE) AS undiscovered,
                (SELECT COALESCE(SUM(total_found),0) FROM server_collection) AS total_found,
                (SELECT COUNT(DISTINCT user_id) FROM inventory) AS unique_users,
                (SELECT COALESCE(SUM(total_messages),0) FROM user_profiles) AS total_messages,
                (SELECT name FROM prizes ORDER BY chance DESC LIMIT 1) AS rarest_prize,
                (SELECT name FROM prizes ORDER BY chance ASC LIMIT 1) AS commonest_prize,
                (SELECT prize_name FROM server_collection WHERE discovered=TRUE ORDER BY total_found DESC LIMIT 1) AS most_found_prize,
                (SELECT COUNT(DISTINCT user_id) FROM inventory WHERE last_found_at>=NOW()-INTERVAL '24 hours') AS active_today,
                (SELECT COUNT(*) FROM inventory WHERE last_found_at>=NOW()-INTERVAL '24 hours') AS rolls_today
            """); return dict(cur.fetchone())

def db_get_profile(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM user_profiles WHERE user_id=%s", (user_id,)); return cur.fetchone()

def db_upsert_profile(user_id, **kwargs):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO user_profiles (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
            for k, v in kwargs.items():
                cur.execute(f"UPDATE user_profiles SET {k}=%s WHERE user_id=%s", (v, user_id))
        conn.commit()

def db_user_inventory_summary(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(DISTINCT i.prize_name) as unique_prizes,
                COALESCE(SUM(i.quantity),0) as total_found,
                MIN(i.first_found_at) as first_find, MAX(i.last_found_at) as last_find
                FROM inventory i WHERE i.user_id=%s""", (user_id,))
            row = cur.fetchone()
            cur.execute("""SELECT i.prize_name,p.chance FROM inventory i JOIN prizes p ON i.prize_name=p.name
                WHERE i.user_id=%s ORDER BY p.chance DESC LIMIT 1""", (user_id,))
            return {**(row or {}), "best_prize": cur.fetchone()}

def db_compare_inventories(u1, u2):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT prize_name FROM inventory WHERE user_id=%s", (u1,))
            s1 = {r["prize_name"] for r in cur.fetchall()}
            cur.execute("SELECT prize_name FROM inventory WHERE user_id=%s", (u2,))
            s2 = {r["prize_name"] for r in cur.fetchall()}
    return {"shared": s1 & s2, "only_u1": s1 - s2, "only_u2": s2 - s1}

def db_recent_finds(limit=10):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT i.user_id,i.prize_name,i.last_found_at,p.chance
                FROM inventory i JOIN prizes p ON i.prize_name=p.name
                ORDER BY i.last_found_at DESC LIMIT %s""", (limit,)); return cur.fetchall()

def db_get_inventory(user_id): return db_get_inventory_sorted(user_id, "rarity")

# ─── Bot Setup ────────────────────────────────────────────────────────────────
TOKEN = os.getenv('DISCORD_TOKEN') or exit("ERROR: DISCORD_TOKEN missing!")
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='-', intents=intents, help_command=None)

# ─── Constants ────────────────────────────────────────────────────────────────
ADMIN_ROLE_ID = 920309927375933490

# ─── Rarity System ────────────────────────────────────────────────────────────
def get_rarity_info(chance):
    if chance >= 1_000_000_000: return ("MYTHIC",    0x2B0F3D, True,  True,  "mythic")
    if chance >= 100_000_000:   return ("LEGENDARY", 0xFF6B6B, True,  False, "legendary")
    if chance >= 10_000_000:    return ("EPIC+",     0x4A90D9, False, False, "epic_plus")
    if chance >= 1_000_000:     return ("EPIC",      0xC97FD4, False, False, "epic")
    if chance >= 100_000:       return ("RARE+",     0x5EC4B8, False, False, "rare_plus")
    if chance >= 10_000:        return ("RARE",      0xF4A23C, False, False, "rare")
    return                             ("COMMON",    0x8B9099, False, False, "common")

TIER_BADGE   = {"mythic":"◈ MYTHIC","legendary":"◆ LEGENDARY","epic_plus":"◇ EPIC+",
                "epic":"◇ EPIC","rare_plus":"○ RARE+","rare":"○ RARE","common":"· COMMON"}
RARITY_EMOJI = {"mythic":"🌑","legendary":"💎","epic_plus":"💙","epic":"💜",
                "rare_plus":"🩵","rare":"🟠","common":"⚪"}
ROLL_HEADER  = {"mythic":"✦  A mythic prize has emerged  ✦","legendary":"✦  A legendary prize appears  ✦",
                "epic_plus":"A powerful prize revealed","epic":"An epic prize revealed",
                "rare_plus":"A rare prize discovered","rare":"A rare prize discovered","common":"You found a prize"}
ROLL_FRAMES  = ["❔  ·  ❔  ·  ❔","🌀  ·  ❔  ·  ❔","🌀  ·  🎯  ·  ❔","🌀  ·  🎯  ·  ✨"]
INV_SORT_OPTIONS = {
    "rarity":   ("Rarity (rarest first)", "p.chance DESC NULLS LAST"),
    "quantity": ("Quantity (most first)",  "i.quantity DESC"),
    "name":     ("Name (A–Z)",             "i.prize_name ASC"),
    "recent":   ("Recently found",         "i.last_found_at DESC NULLS LAST"),
    "oldest":   ("First found",            "i.first_found_at ASC"),
}

# ─── Helpers ──────────────────────────────────────────────────────────────────
def is_admin(m): return m.guild_permissions.administrator or any(r.id == ADMIN_ROLE_ID for r in m.roles)
def fmt_dt(dt):  return dt.strftime("%d %b %Y, %H:%M UTC") if dt and hasattr(dt,'strftime') else "—"
def fmt_dt_s(dt): return dt.strftime("%d %b %Y") if dt and hasattr(dt,'strftime') else "—"
def rarity_bar(chance):
    filled = sum(1 for t in [10_000,100_000,1_000_000,10_000_000,100_000_000,1_000_000_000] if chance >= t)
    return "▰"*filled + "▱"*(6-filled)
def owner_check(interaction, uid): return interaction.user.id == uid

def parse_bet(amount_str: str, balance: int) -> int | None:
    """Parse bet amount, supporting 'all', 'half', 'k', 'm' suffixes. Returns None on error."""
    s = amount_str.lower().strip()
    if s in ("all", "max"): return min(balance, MAX_BET)
    if s == "half": return min(balance // 2, MAX_BET)
    try:
        if s.endswith("k"): return int(float(s[:-1]) * 1_000)
        if s.endswith("m"): return int(float(s[:-1]) * 1_000_000)
        return int(s)
    except ValueError:
        return None

def build_daily_store_embed(rows):
    e = discord.Embed(title="🛒  Daily Store", color=0xF4D03F, timestamp=datetime.now(timezone.utc))
    now_utc = datetime.now(timezone.utc)
    next_reroll = now_utc.replace(hour=17, minute=0, second=0, microsecond=0)
    if now_utc.hour >= 17: next_reroll += timedelta(days=1)
    ts = int(next_reroll.timestamp())
    e.description = f"Restocks daily at midnight UTC+7  ·  Next reroll <t:{ts}:R>\n\u200b"
    for i, row in enumerate(rows):
        item = ITEMS.get(row["item_id"])
        if not item: continue
        tier = item["tier"]
        stock_bar = "▰" * row["stock"] + "▱" * max(0, DAILY_STOCK.get(tier,3) - row["stock"])
        e.add_field(
            name=f"{ITEM_TIER_EMOJI[tier]}  `{i+1}.`  {item['name']}",
            value=(f"`{ITEM_TIER_BADGE[tier]}`\n{item['desc']}\n"
                   f"**{row['price']:,} ₡**  ·  Stock: `{stock_bar}` {row['stock']}"),
            inline=False)
    e.set_footer(text="Use -buy <slot number> to purchase")
    return e

# ─── Embeds ───────────────────────────────────────────────────────────────────
def build_spinning_embed(frame, color):
    e = discord.Embed(description=f"```\n{frame}\n```", color=color)
    e.set_footer(text="Rolling…"); return e

def build_roll_embed(prize, user):
    chance = prize["chance"]
    label, color, _, _, tier = get_rarity_info(chance)
    desc = prize["roll_message"].replace("{user}", user.mention).replace("{prize}", f"**{prize['name']}**")
    e = discord.Embed(title=f"{RARITY_EMOJI[tier]}  {ROLL_HEADER[tier]}", description=f"{desc}\n\u200b", color=color)
    e.add_field(name="Prize",           value=f"**{prize['name']}**",   inline=True)
    e.add_field(name="Tier",            value=f"`{TIER_BADGE[tier]}`",  inline=True)
    e.add_field(name="Odds",            value=f"1 in {chance:,}",       inline=True)
    e.add_field(name="Rarity spectrum", value=f"`{rarity_bar(chance)}`",inline=False)
    if prize.get("description"):
        e.add_field(name="About", value=f"*{prize['description']}*", inline=False)
    e.set_footer(text=f"Rolled by {user.display_name}  ·  {datetime.now(timezone.utc).strftime('%d %b %Y, %H:%M UTC')}",
                 icon_url=user.display_avatar.url)
    if prize.get("image"): e.set_image(url=prize["image"])
    return e

def build_stats_embed(s):
    total, disc = s.get("total_prizes",0), s.get("discovered",0)
    pct = round(disc/total*100,1) if total else 0
    bar = "▰"*int(pct/10) + "▱"*(10-int(pct/10))
    e = discord.Embed(title="Server statistics", color=0x5865F2, timestamp=datetime.now(timezone.utc))
    e.description = f"`{bar}`  **{disc}/{total}** prizes discovered  ({pct}%)"
    e.add_field(name="Rolls",    value=f"All-time  **{s.get('total_found',0):,}**\nToday  **{s.get('rolls_today',0):,}**", inline=True)
    e.add_field(name="Players",  value=f"Ever played  **{s.get('unique_users',0):,}**\nActive today  **{s.get('active_today',0):,}**", inline=True)
    e.add_field(name="Messages", value=f"**{s.get('total_messages',0):,}**", inline=True)
    for key, label in [("rarest_prize","Rarest prize"), ("commonest_prize","Most common prize")]:
        if s.get(key):
            p = db_get_prize(s[key])
            if p:
                _, _, _, _, tier = get_rarity_info(p["chance"])
                e.add_field(name=label, value=f"{RARITY_EMOJI[tier]} **{p['name']}**\n`{TIER_BADGE[tier]}`  ·  1/{p['chance']:,}", inline=True)
    if s.get("most_found_prize"):
        d = db_get_disc(s["most_found_prize"])
        e.add_field(name="Most-found prize", value=f"**{s['most_found_prize']}**  ·  {d.get('total_found','?')}× rolled", inline=True)
    e.set_footer(text="Stats refresh in real time"); return e

# ─── Animated Roll ────────────────────────────────────────────────────────────
async def do_animated_roll(channel, prize, user):
    _, color, ping_e, announce, tier = get_rarity_info(prize["chance"])
    msg = await channel.send(embed=build_spinning_embed(ROLL_FRAMES[0], color))
    for i, frame in enumerate(ROLL_FRAMES[1:], 1):
        await asyncio.sleep(0.5 if i < 3 else 0.8)
        await msg.edit(embed=build_spinning_embed(frame, color))
    await asyncio.sleep(1.0)
    final = build_roll_embed(prize, user)
    await msg.edit(content="@everyone" if ping_e else "", embed=final)
    if announce:
        ch = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
        if ch: await ch.send(content="@everyone  🌑  **A mythic prize has just been rolled!**", embed=final)

# ─── Casino Helpers ───────────────────────────────────────────────────────────
def card_value(card):
    rank = card[:-1]
    if rank in ("J","Q","K"): return 10
    if rank == "A": return 11
    return int(rank)

def hand_value(hand):
    total = sum(card_value(c) for c in hand)
    aces = sum(1 for c in hand if c[:-1] == "A")
    while total > 21 and aces:
        total -= 10; aces -= 1
    return total

def hand_str(hand, hide_second=False):
    if hide_second:
        return f"`{hand[0]}`  `??`"
    return "  ".join(f"`{c}`" for c in hand)

def new_deck():
    suits = ["♠","♥","♦","♣"]
    ranks = ["2","3","4","5","6","7","8","9","10","J","Q","K","A"]
    deck = [f"{r}{s}" for r in ranks for s in suits] * 6
    random.shuffle(deck)
    return deck

def spin_slots():
    symbols = [s[0] for s in SLOT_SYMBOLS]
    weights = [s[1] for s in SLOT_SYMBOLS]
    return random.choices(symbols, weights=weights, k=3)

def slot_payout(reels, bet):
    if reels[0] == reels[1] == reels[2]:
        sym = reels[0]
        mult = next(s[2] for s in SLOT_SYMBOLS if s[0] == sym)
        return bet * mult, f"**JACKPOT!** ×{mult}"
    if reels[0] == reels[1] or reels[1] == reels[2]:
        return int(bet * 1.5), "Two in a row  ×1.5"
    return 0, "No match"

def roulette_resolve(bet_type: str, number: int):
    """Returns multiplier or 0. bet_type: red/black/even/odd/0-36"""
    reds = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    if bet_type.isdigit():
        n = int(bet_type)
        return 35 if number == n else 0
    if bet_type == "red":    return 2 if number in reds and number != 0 else 0
    if bet_type == "black":  return 2 if number not in reds and number != 0 else 0
    if bet_type == "even":   return 2 if number != 0 and number % 2 == 0 else 0
    if bet_type == "odd":    return 2 if number % 2 == 1 else 0
    if bet_type == "low":    return 2 if 1 <= number <= 18 else 0
    if bet_type == "high":   return 2 if 19 <= number <= 36 else 0
    return 0

# ─── Active Blackjack Sessions (in-memory) ───────────────────────────────────
_bj_sessions: dict[int, dict] = {}  # user_id -> session

# ─── Modals ───────────────────────────────────────────────────────────────────
class PrizeMakerModal(discord.ui.Modal, title="Create / Edit Prize"):
    prize_name = discord.ui.TextInput(label="Prize name (unique)", placeholder="Golden Crown", max_length=100)
    image_url  = discord.ui.TextInput(label="Image URL (optional)", placeholder="https://i.imgur.com/example.png", required=False, max_length=500)
    chance     = discord.ui.TextInput(label="Chance — 1 in X  (e.g. 10000)", placeholder="10000", max_length=12)
    roll_msg   = discord.ui.TextInput(label="Roll message  •  use {user} and {prize}", placeholder="{user} rolled {prize}!", max_length=300, style=discord.TextStyle.paragraph)
    desc       = discord.ui.TextInput(label="Description", placeholder="A rare golden crown...", max_length=200, required=False, style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction):
        try:
            cv = int(self.chance.value.strip())
            if cv < 1: raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ Chance must be a positive whole number.", ephemeral=True); return
        name = self.prize_name.value.strip()
        data = {"name":name,"image":self.image_url.value.strip() or None,"chance":cv,
                "roll_message":self.roll_msg.value.strip(),"description":self.desc.value.strip() or None}
        action = db_upsert_prize(data)
        _, color, ping_e, announce, tier = get_rarity_info(cv)
        e = discord.Embed(title=f"Prize {action.lower()}", color=color)
        e.add_field(name="Name",     value=name,                    inline=True)
        e.add_field(name="Tier",     value=f"`{TIER_BADGE[tier]}`", inline=True)
        e.add_field(name="Odds",     value=f"1 in {cv:,}",          inline=True)
        e.add_field(name="Spectrum", value=f"`{rarity_bar(cv)}`",   inline=False)
        preview = data["roll_message"].replace("{user}", interaction.user.mention).replace("{prize}", f"**{name}**")
        e.add_field(name="Message preview", value=preview, inline=False)
        flags = (["Pings @everyone"] if ping_e else []) + (["Posts to announcement channel"] if announce else [])
        if flags: e.add_field(name="⚠️ Effects", value="  ·  ".join(flags), inline=False)
        if data["image"]: e.set_thumbnail(url=data["image"])
        await interaction.response.send_message(embed=e, ephemeral=True)

class BioModal(discord.ui.Modal, title="Edit your bio"):
    bio = discord.ui.TextInput(label="Bio", placeholder="Tell the server about yourself…", max_length=200, required=False, style=discord.TextStyle.paragraph)
    async def on_submit(self, interaction):
        db_upsert_profile(interaction.user.id, bio=self.bio.value.strip() or None)
        await interaction.response.send_message("✅ Bio updated.", ephemeral=True)

# ─── Selects ──────────────────────────────────────────────────────────────────
def prize_options(prizes):
    return [discord.SelectOption(label=p["name"][:100],
        description=f"{TIER_BADGE.get(get_rarity_info(p['chance'])[4],'')}  ·  1/{p['chance']:,}",
        value=p["name"]) for p in list(prizes)[:25]]

class DeleteSelect(discord.ui.Select):
    def __init__(self, prizes): super().__init__(placeholder="Select a prize to delete…", options=prize_options(prizes))
    async def callback(self, i):
        db_delete_prize(self.values[0]); await i.response.send_message(f"🗑️ **{self.values[0]}** deleted.", ephemeral=True)

class PreviewSelect(discord.ui.Select):
    def __init__(self, prizes): super().__init__(placeholder="Select a prize to preview…", options=prize_options(prizes))
    async def callback(self, i):
        p = db_get_prize(self.values[0])
        if not p: await i.response.send_message("❌ Not found.", ephemeral=True); return
        await i.response.send_message(embed=build_roll_embed(p, i.user), ephemeral=True)

class EditSelect(discord.ui.Select):
    def __init__(self, prizes): super().__init__(placeholder="Select a prize to edit…", options=prize_options(prizes))
    async def callback(self, i):
        p = db_get_prize(self.values[0]); modal = PrizeMakerModal()
        if p:
            modal.prize_name.default = p["name"]; modal.image_url.default = p.get("image") or ""
            modal.chance.default = str(p["chance"]); modal.roll_msg.default = p["roll_message"]
            modal.desc.default = p.get("description") or ""
        await i.response.send_modal(modal)

class ShowcaseSelect(discord.ui.Select):
    def __init__(self, entries):
        super().__init__(placeholder="Choose your showcase prize…",
            options=[discord.SelectOption(label=e["prize_name"][:100], description=f"Owned {e['quantity']}×", value=e["prize_name"])
                     for e in list(entries)[:25]])
    async def callback(self, i):
        db_upsert_profile(i.user.id, showcase_prize=self.values[0])
        await i.response.send_message(f"✅ Showcase set to **{self.values[0]}**.", ephemeral=True)

def _make_view(cls, items):
    v = discord.ui.View(timeout=60); v.add_item(cls(items)); return v

# ─── Inventory Sort Select ────────────────────────────────────────────────────
class InventorySortSelect(discord.ui.Select):
    def __init__(self, user, current_sort, owner_id):
        super().__init__(placeholder="Sort by…", row=0,
            options=[discord.SelectOption(label=label, value=k, default=(k==current_sort))
                     for k,(label,_) in INV_SORT_OPTIONS.items()])
        self.inv_user = user; self.owner_id = owner_id
    async def callback(self, i):
        if not owner_check(i, self.owner_id): await i.response.send_message("This isn't your inventory panel.", ephemeral=True); return
        entries = db_get_inventory_sorted(self.inv_user.id, self.values[0])
        v = InventoryView(list(entries), self.inv_user, sort=self.values[0], owner_id=self.owner_id)
        await i.response.edit_message(embed=v.build_embed(), view=v)

# ─── Leaderboard Type Select ──────────────────────────────────────────────────
class LeaderboardTypeSelect(discord.ui.Select):
    def __init__(self, guild, current, owner_id):
        super().__init__(placeholder="Switch leaderboard…", options=[
            discord.SelectOption(label="Collected Stats", value="collected", default=(current=="collected")),
            discord.SelectOption(label="Rarest Finds",    value="rarest",   default=(current=="rarest")),
            discord.SelectOption(label="Most Messages",   value="messages", default=(current=="messages")),
            discord.SelectOption(label="Richest",         value="credits",  default=(current=="credits")),
            discord.SelectOption(label="Casino — Top Winners", value="casino", default=(current=="casino")),
        ])
        self.guild = guild; self.owner_id = owner_id
    async def callback(self, i):
        if not owner_check(i, self.owner_id): await i.response.send_message("This isn't your leaderboard panel.", ephemeral=True); return
        v = LeaderboardView(self.guild, lb_type=self.values[0], owner_id=self.owner_id)
        await i.response.edit_message(embed=v.build_embed(), view=v)

# ─── Prize Maker Panel ────────────────────────────────────────────────────────
class PrizeMakerView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    async def _check(self, i):
        if not is_admin(i.user): await i.response.send_message("❌ No permission.", ephemeral=True); return False
        return True

    @discord.ui.button(label="New prize",    style=discord.ButtonStyle.success,   row=0)
    async def add_prize(self, i, b):
        if not await self._check(i): return
        await i.response.send_modal(PrizeMakerModal())

    @discord.ui.button(label="Edit prize",   style=discord.ButtonStyle.primary,   row=0)
    async def edit_prize(self, i, b):
        if not await self._check(i): return
        prizes = db_load_prizes()
        if not prizes: await i.response.send_message("No prizes yet.", ephemeral=True); return
        await i.response.send_message("Select a prize to edit:", view=_make_view(EditSelect, prizes), ephemeral=True)

    @discord.ui.button(label="Delete prize", style=discord.ButtonStyle.danger,    row=0)
    async def delete_prize(self, i, b):
        if not await self._check(i): return
        prizes = db_load_prizes()
        if not prizes: await i.response.send_message("No prizes to delete.", ephemeral=True); return
        await i.response.send_message("Select a prize to delete:", view=_make_view(DeleteSelect, prizes), ephemeral=True)

    @discord.ui.button(label="List all",     style=discord.ButtonStyle.secondary, row=1)
    async def list_prizes(self, i, b):
        if not await self._check(i): return
        prizes = db_load_prizes()
        if not prizes: await i.response.send_message("No prizes yet.", ephemeral=True); return
        e = discord.Embed(title="All prizes", color=0x5865F2)
        e.description = f"{len(prizes)} prizes in the pool\n\u200b"
        for p in prizes:
            _, _, _, _, tier = get_rarity_info(p["chance"])
            e.add_field(name=p["name"], value=f"`{TIER_BADGE[tier]}`  ·  1/{p['chance']:,}\n*{p.get('description') or 'No description'}*", inline=False)
        await i.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="Preview roll", style=discord.ButtonStyle.secondary, row=1)
    async def preview_roll(self, i, b):
        if not await self._check(i): return
        prizes = db_load_prizes()
        if not prizes: await i.response.send_message("No prizes to preview.", ephemeral=True); return
        await i.response.send_message("Select a prize to preview:", view=_make_view(PreviewSelect, prizes), ephemeral=True)

    @discord.ui.button(label="Stats",        style=discord.ButtonStyle.secondary, row=1)
    async def stats(self, i, b):
        if not await self._check(i): return
        await i.response.send_message(embed=build_stats_embed(db_stats()), ephemeral=True)

# ─── Inventory View ───────────────────────────────────────────────────────────
class InventoryView(discord.ui.View):
    def __init__(self, entries, user, page=0, sort="rarity", owner_id=None):
        super().__init__(timeout=120)
        self.entries = entries; self.user = user; self.page = page
        self.sort = sort; self.owner_id = owner_id or user.id
        self.per = 6; self.pages = max(1, (len(entries)+self.per-1)//self.per)
        self.add_item(InventorySortSelect(user, sort, self.owner_id)); self._sync()

    def _sync(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.pages - 1

    def build_embed(self):
        chunk = self.entries[self.page*self.per:(self.page+1)*self.per]
        s = db_user_inventory_summary(self.user.id)
        total, unique = int(s.get("total_found",0) or 0), int(s.get("unique_prizes",0) or 0)
        tp = len(get_prizes_cached()); comp = round(unique/tp*100,1) if tp else 0
        bar = "▰"*int(comp/10) + "▱"*(10-int(comp/10))
        e = discord.Embed(title=f"{self.user.display_name}'s collection", color=0x5865F2)
        e.description = (f"`{bar}`  **{comp}%** complete  ({unique}/{tp} unique · {total} total)\n"
                         f"Sorted by *{INV_SORT_OPTIONS.get(self.sort,('',))[0]}*  ·  page {self.page+1}/{self.pages}")
        for entry in chunk:
            chance = entry.get("chance")
            _, _, _, _, tier = get_rarity_info(chance) if chance else (*([None]*4), "common")
            e.add_field(name=f"{RARITY_EMOJI[tier]}  {entry['prize_name']}",
                value=(f"`{TIER_BADGE[tier]}`\nOwned  **{entry['quantity']}×**\n"
                       f"First found  {fmt_dt_s(entry.get('first_found_at'))}\n"
                       f"Last found  {fmt_dt_s(entry.get('last_found_at'))}"), inline=True)
        e.set_thumbnail(url=self.user.display_avatar.url); return e

    @discord.ui.button(label="◀  Prev", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, i, b):
        if not owner_check(i, self.owner_id): await i.response.send_message("This isn't your inventory panel.", ephemeral=True); return
        self.page -= 1; self._sync(); await i.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next  ▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, i, b):
        if not owner_check(i, self.owner_id): await i.response.send_message("This isn't your inventory panel.", ephemeral=True); return
        self.page += 1; self._sync(); await i.response.edit_message(embed=self.build_embed(), view=self)

# ─── Collection View ──────────────────────────────────────────────────────────
class CollectionView(discord.ui.View):
    def __init__(self, all_prizes, discoveries, page=0, owner_id=None):
        super().__init__(timeout=120)
        self.all_prizes = all_prizes; self.discoveries = discoveries
        self.page = page; self.owner_id = owner_id
        self.per = 6; self.pages = max(1, (len(all_prizes)+self.per-1)//self.per)
        self._sync()

    def _sync(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.pages - 1

    def build_embed(self):
        chunk = self.all_prizes[self.page*self.per:(self.page+1)*self.per]
        total = len(self.all_prizes)
        disc = sum(1 for d in self.discoveries.values() if d.get("discovered"))
        pct = round(disc/total*100,1) if total else 0
        bar = "▰"*int(pct/10) + "▱"*(10-int(pct/10))
        e = discord.Embed(title="Server collection", color=0x5865F2)
        e.description = f"`{bar}`  **{pct}%**  ({disc}/{total} discovered)\nPage {self.page+1}/{self.pages}"
        for prize in chunk:
            d = self.discoveries.get(prize["name"], {})
            if d.get("discovered"):
                _, _, _, _, tier = get_rarity_info(prize["chance"])
                e.add_field(name=f"{RARITY_EMOJI[tier]}  {prize['name']}",
                    value=(f"`{TIER_BADGE[tier]}`  ·  1/{prize['chance']:,}\n"
                           f"*{prize.get('description') or 'No description'}*\n"
                           f"First found by **{d.get('first_user','—')}**\n"
                           f"{fmt_dt_s(d.get('first_at'))}  ·  {d.get('total_found',0)}× total"), inline=True)
            else:
                e.add_field(name="❔  Unknown", value="*Not yet discovered.*", inline=True)
        return e

    @discord.ui.button(label="◀  Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, i, b):
        if self.owner_id and not owner_check(i, self.owner_id): await i.response.send_message("This isn't your collection panel.", ephemeral=True); return
        self.page -= 1; self._sync(); await i.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next  ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, i, b):
        if self.owner_id and not owner_check(i, self.owner_id): await i.response.send_message("This isn't your collection panel.", ephemeral=True); return
        self.page += 1; self._sync(); await i.response.edit_message(embed=self.build_embed(), view=self)

# ─── Leaderboard View ─────────────────────────────────────────────────────────
class LeaderboardView(discord.ui.View):
    def __init__(self, guild, lb_type="collected", owner_id=None):
        super().__init__(timeout=120)
        self.guild = guild; self.lb_type = lb_type; self.owner_id = owner_id
        self.add_item(LeaderboardTypeSelect(guild, lb_type, owner_id))

    def build_embed(self):
        medals = ["🥇","🥈","🥉"] + [f"`{n}`" for n in range(4,11)]
        if self.lb_type == "collected":
            results = db_leaderboard_collected_stats()
            e = discord.Embed(title="Collected Stats", color=0xF4A23C)
            e.description = "Total sum of all prize rarities collected.\n*(e.g. rolling a 1/200 and 1/100 = score of 300)*\n\u200b"
            for i, r in enumerate(results):
                m = self.guild.get_member(r["user_id"])
                name = m.display_name if m else f"User {r['user_id']}"
                e.add_field(name=f"{medals[i]}  {name}",
                    value=f"Score  **{int(r['collected_score']):,}**  ·  {int(r['total']):,} prizes rolled", inline=False)
        elif self.lb_type == "rarest":
            results = db_leaderboard_rarest_finds()
            e = discord.Embed(title="Rarest Finds", color=0xC97FD4)
            e.description = "Ranked by the single rarest prize each player has rolled.\n\u200b"
            for i, r in enumerate(results):
                m = self.guild.get_member(r["user_id"])
                name = m.display_name if m else f"User {r['user_id']}"
                chance = int(r["rarest_chance"])
                _, _, _, _, tier = get_rarity_info(chance)
                e.add_field(name=f"{medals[i]}  {name}",
                    value=f"{RARITY_EMOJI[tier]}  **{r['rarest_prize_name']}**  `{TIER_BADGE[tier]}`\n1 in {chance:,}", inline=False)
        elif self.lb_type == "messages":
            results = db_leaderboard_messages()
            e = discord.Embed(title="Most Messages", color=0x5EC4B8)
            e.description = "Ranked by total messages sent.\n\u200b"
            for i, r in enumerate(results):
                m = self.guild.get_member(r["user_id"])
                name = m.display_name if m else f"User {r['user_id']}"
                e.add_field(name=f"{medals[i]}  {name}", value=f"**{int(r['total_messages']):,}** messages", inline=False)
        elif self.lb_type == "casino":
            results = db_casino_leaderboard()
            e = discord.Embed(title="🎰  Casino — Top Winners", color=CASINO_COLOR)
            e.description = "Ranked by net profit in casino games.\n\u200b"
            for i, r in enumerate(results):
                m = self.guild.get_member(r["user_id"])
                name = m.display_name if m else f"User {r['user_id']}"
                net = int(r["net"])
                sign = "+" if net >= 0 else ""
                e.add_field(name=f"{medals[i]}  {name}",
                    value=(f"Net  **{sign}{net:,} ₡**  ·  {int(r['games_played'])} games\n"
                           f"Biggest win  **{int(r['biggest_win']):,} ₡**"), inline=False)
        else:  # credits
            results = db_leaderboard_credits()
            e = discord.Embed(title="Richest Players", color=0xF1C40F)
            e.description = "Ranked by total credits (wallet + bank).\n\u200b"
            for i, r in enumerate(results):
                m = self.guild.get_member(r["user_id"])
                name = m.display_name if m else f"User {r['user_id']}"
                e.add_field(name=f"{medals[i]}  {name}",
                    value=f"**{int(r['total']):,} ₡** total  ·  👛 {int(r['balance']):,}  ·  🏦 {int(r['bank']):,}", inline=False)
        e.set_footer(text="Use the dropdown to switch view"); return e

# ─── Profile View ─────────────────────────────────────────────────────────────
class ProfileView(discord.ui.View):
    def __init__(self, target, is_self, owner_id):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        if not is_self:
            self.edit_bio_btn.disabled = True; self.set_showcase_btn.disabled = True

    @discord.ui.button(label="Edit bio",     style=discord.ButtonStyle.primary)
    async def edit_bio_btn(self, i, b):
        if not owner_check(i, self.owner_id): await i.response.send_message("This isn't your profile.", ephemeral=True); return
        await i.response.send_modal(BioModal())

    @discord.ui.button(label="Set showcase", style=discord.ButtonStyle.secondary)
    async def set_showcase_btn(self, i, b):
        if not owner_check(i, self.owner_id): await i.response.send_message("This isn't your profile.", ephemeral=True); return
        entries = db_get_inventory(i.user.id)
        if not entries: await i.response.send_message("You have no prizes to showcase yet.", ephemeral=True); return
        await i.response.send_message("Choose your showcase prize:", view=_make_view(ShowcaseSelect, list(entries)), ephemeral=True)

# ─── Daily Store View ─────────────────────────────────────────────────────────
class DailyStoreView(discord.ui.View):
    def __init__(self, rows, owner_id):
        super().__init__(timeout=120)
        self.rows = rows; self.owner_id = owner_id

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)
    async def refresh(self, i, b):
        rows = db_get_daily_store()
        await i.response.edit_message(embed=build_daily_store_embed(rows), view=DailyStoreView(rows, self.owner_id))

# ─── Blackjack View ───────────────────────────────────────────────────────────
class BlackjackView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id

    def _check(self, i):
        return i.user.id == self.user_id

    @discord.ui.button(label="🃏 Hit",      style=discord.ButtonStyle.primary,   row=0)
    async def hit_btn(self, i, b):
        if not self._check(i): await i.response.send_message("This isn't your game!", ephemeral=True); return
        sess = _bj_sessions.get(self.user_id)
        if not sess: await i.response.edit_message(content="Session expired.", view=None); return
        sess["player"].append(sess["deck"].pop())
        pv = hand_value(sess["player"])
        if pv > 21:
            # bust
            bet = sess["bet"]
            db_add_credits(self.user_id, 0)  # no refund
            db_record_casino(self.user_id, bet, -bet)
            _bj_sessions.pop(self.user_id, None)
            e = _bj_embed(sess, bust=True)
            await i.response.edit_message(embed=e, view=None)
        else:
            e = _bj_embed(sess)
            await i.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="🛑 Stand",    style=discord.ButtonStyle.secondary, row=0)
    async def stand_btn(self, i, b):
        if not self._check(i): await i.response.send_message("This isn't your game!", ephemeral=True); return
        await _bj_resolve(i, self.user_id, action="stand")

    @discord.ui.button(label="⚡ Double",   style=discord.ButtonStyle.success,   row=0)
    async def double_btn(self, i, b):
        if not self._check(i): await i.response.send_message("This isn't your game!", ephemeral=True); return
        sess = _bj_sessions.get(self.user_id)
        if not sess: await i.response.edit_message(content="Session expired.", view=None); return
        # Double down: pay extra bet then force one card + stand
        extra = sess["bet"]
        w = db_get_wallet(self.user_id)
        if w["balance"] < extra:
            await i.response.send_message("❌ Not enough credits to double down!", ephemeral=True); return
        db_deduct_credits(self.user_id, extra)
        sess["bet"] *= 2
        sess["player"].append(sess["deck"].pop())
        await _bj_resolve(i, self.user_id, action="double")

def _bj_embed(sess, bust=False, result=None):
    pv = hand_value(sess["player"])
    dv = hand_value(sess["dealer"])
    hide = (result is None and not bust)
    e = discord.Embed(title="🃏  Blackjack", color=CASINO_COLOR)
    e.add_field(name=f"Your hand  ({pv})",
                value=hand_str(sess["player"]),
                inline=False)
    if hide:
        e.add_field(name="Dealer's hand  (?)",
                    value=hand_str(sess["dealer"], hide_second=True),
                    inline=False)
    else:
        e.add_field(name=f"Dealer's hand  ({dv})",
                    value=hand_str(sess["dealer"]),
                    inline=False)
    e.add_field(name="Bet", value=f"**{sess['bet']:,} ₡**", inline=True)
    if bust:
        e.add_field(name="Result", value="💥 **BUST!**  You lose.", inline=True)
        e.color = 0xFF4444
    elif result == "win":
        payout = sess["bet"] * (2 if not sess.get("bj") else 3) if not sess.get("bj") else int(sess["bet"] * 2.5)
        e.add_field(name="Result", value=f"✅ **WIN!**  +{payout - sess['bet'] if not sess.get('bj') else int(sess['bet']*1.5):,} ₡", inline=True)
        e.color = 0x2ECC71
    elif result == "lose":
        e.add_field(name="Result", value=f"❌ **LOSE!**  -{sess['bet']:,} ₡", inline=True)
        e.color = 0xFF4444
    elif result == "push":
        e.add_field(name="Result", value="🤝 **PUSH!**  Bet returned.", inline=True)
        e.color = 0x8B9099
    elif result == "blackjack":
        e.add_field(name="Result", value=f"🌟 **BLACKJACK!**  +{int(sess['bet']*1.5):,} ₡", inline=True)
        e.color = 0xF1C40F
    else:
        e.set_footer(text="Hit · Stand · Double Down")
    return e

async def _bj_resolve(interaction, user_id, action="stand"):
    sess = _bj_sessions.get(user_id)
    if not sess:
        await interaction.response.edit_message(content="Session expired.", view=None); return
    # Dealer draws to 17
    while hand_value(sess["dealer"]) < 17:
        sess["dealer"].append(sess["deck"].pop())
    pv = hand_value(sess["player"])
    dv = hand_value(sess["dealer"])
    bet = sess["bet"]
    if dv > 21 or pv > dv:
        payout = int(bet * 2.5) if sess.get("bj") else bet * 2
        db_add_credits(user_id, payout)
        db_record_casino(user_id, bet, payout - bet)
        result = "blackjack" if sess.get("bj") else "win"
    elif pv == dv:
        db_add_credits(user_id, bet)  # refund
        db_record_casino(user_id, bet, 0)
        result = "push"
    else:
        db_record_casino(user_id, bet, -bet)
        result = "lose"
    e = _bj_embed(sess, result=result)
    _bj_sessions.pop(user_id, None)
    await interaction.response.edit_message(embed=e, view=None)

# ─── on_message ───────────────────────────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot: return

    uid  = message.author.id
    now  = datetime.now(timezone.utc)

    # ── Custom replies (check before spam guard) ──
    if not message.content.startswith(bot.command_prefix):
        reply = db_check_custom_reply(uid, message.content)
        if reply:
            await message.channel.send(reply)

    spam = db_check_spam(uid, message.content)

    if not spam:
        skip_val, skip_rem = db_tick_effect(uid, "skip_msgs", decrement=1)
        db_ensure_wallet(uid)
        profile = db_get_profile(uid)
        total_msgs = (profile["total_messages"] + 1) if profile else 1
        credits_earned = compute_credit_award(total_msgs)
        db_add_credits(uid, credits_earned)
        db_apply_bank_interest(uid)
        stack_val, stack_rem = db_tick_effect(uid, "stack_mult", decrement=1)
        roll_attempts = int(stack_val) if stack_val > 0 else 1
        luck_val, luck_rem = db_tick_effect(uid, "luck_bonus", decrement=1)
        luck_bonus = int(luck_val) if luck_val > 0 else 0
        prizes = get_prizes_cached()
        won = False
        for _ in range(roll_attempts):
            if won: break
            for prize in prizes:
                effective_chance = max(1, prize["chance"] - luck_bonus)
                if random.randint(1, effective_chance) == 1:
                    db_record_roll(prize["name"], uid, message.author.display_name, now)
                    await do_animated_roll(message.channel, prize, message.author)
                    won = True; break
        if not won:
            try: db_increment_messages(uid)
            except Exception: pass

    await bot.process_commands(message)

# ─── Scheduled Tasks ──────────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def daily_store_task():
    now = datetime.now(timezone.utc)
    if now.hour == 17 and now.minute == 0:
        rows = db_rotate_store()
        ch = bot.get_channel(GENERAL_CHANNEL_ID)
        if ch:
            e = build_daily_store_embed(rows)
            e.title = "🛒  Daily Store Restocked!"
            await ch.send(content="@everyone  The daily store has refreshed!", embed=e)

@bot.event
async def on_ready():
    init_db()
    if not db_get_daily_store():
        db_rotate_store()
    daily_store_task.start()
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"   {len(db_load_prizes())} prize(s) in database")

# ═══════════════════════════════════════════════════════════════════════════════
# ─── CASINO COMMANDS ──────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def _bet_check(ctx, amount_str: str):
    """Returns (bet, None) or (None, error_msg)."""
    db_ensure_wallet(ctx.author.id)
    w = db_get_wallet(ctx.author.id)
    bet = parse_bet(amount_str, w["balance"])
    if bet is None:
        return None, "❌ Invalid amount. Use a number, `all`, `half`, `10k`, etc."
    if bet < MIN_BET:
        return None, f"❌ Minimum bet is **{MIN_BET:,} ₡**."
    if bet > MAX_BET:
        return None, f"❌ Maximum bet is **{MAX_BET:,} ₡**."
    if w["balance"] < bet:
        return None, f"❌ Not enough credits. You have **{w['balance']:,} ₡**."
    return bet, None

# ── Coinflip ──────────────────────────────────────────────────────────────────
@bot.command(name="coinflip", aliases=["cf","flip"])
async def coinflip_cmd(ctx, amount: str, side: str = "heads"):
    """Bet on heads or tails. -coinflip <amount> [heads|tails]"""
    bet, err = _bet_check(ctx, amount)
    if err: await ctx.send(err); return
    side = side.lower()
    if side not in ("heads","tails","h","t"):
        await ctx.send("❌ Choose `heads` or `tails`."); return
    side = "heads" if side in ("heads","h") else "tails"
    if not db_deduct_credits(ctx.author.id, bet):
        await ctx.send("❌ Could not deduct credits."); return
    result = random.choice(["heads","tails"])
    won = result == side
    payout = bet * 2 if won else 0
    if payout: db_add_credits(ctx.author.id, payout)
    db_record_casino(ctx.author.id, bet, bet if won else -bet)
    coin_emoji = "🪙" if result == "heads" else "🌑"
    color = 0x2ECC71 if won else 0xFF4444
    e = discord.Embed(title=f"{coin_emoji}  Coin Flip", color=color)
    e.add_field(name="Your call",  value=f"**{side.capitalize()}**",   inline=True)
    e.add_field(name="Result",     value=f"**{result.capitalize()}**", inline=True)
    e.add_field(name="Bet",        value=f"**{bet:,} ₡**",             inline=True)
    if won:
        e.add_field(name="Outcome", value=f"✅  **WIN!**  +{bet:,} ₡", inline=False)
    else:
        e.add_field(name="Outcome", value=f"❌  **LOSE!**  -{bet:,} ₡", inline=False)
    e.set_footer(text=f"{ctx.author.display_name}  ·  Wallet: {db_get_wallet(ctx.author.id)['balance']:,} ₡",
                 icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=e)

# ── Slots ─────────────────────────────────────────────────────────────────────
@bot.command(name="slots", aliases=["slot","sl"])
async def slots_cmd(ctx, amount: str):
    """Spin the slot machine. -slots <amount>"""
    bet, err = _bet_check(ctx, amount)
    if err: await ctx.send(err); return
    if not db_deduct_credits(ctx.author.id, bet):
        await ctx.send("❌ Could not deduct credits."); return

    # Animated spin
    e = discord.Embed(title="🎰  Slot Machine", color=CASINO_COLOR)
    e.description = "```\n[ 🌀 | 🌀 | 🌀 ]\n```"
    e.add_field(name="Bet", value=f"**{bet:,} ₡**", inline=True)
    msg = await ctx.send(embed=e)
    await asyncio.sleep(0.8)

    reels = spin_slots()
    payout, label = slot_payout(reels, bet)
    if payout: db_add_credits(ctx.author.id, payout)
    net = payout - bet
    db_record_casino(ctx.author.id, bet, net)

    color = 0x2ECC71 if net > 0 else (0x8B9099 if net == 0 else 0xFF4444)
    e2 = discord.Embed(title="🎰  Slot Machine", color=color)
    e2.description = f"```\n[ {reels[0]} | {reels[1]} | {reels[2]} ]\n```"
    e2.add_field(name="Bet",    value=f"**{bet:,} ₡**",                             inline=True)
    e2.add_field(name="Result", value=label,                                          inline=True)
    if net > 0:
        e2.add_field(name="Payout", value=f"✅  **+{net:,} ₡**  (×{payout//bet})", inline=False)
    elif net == 0:
        e2.add_field(name="Payout", value=f"🤝  Break even",                         inline=False)
    else:
        e2.add_field(name="Payout", value=f"❌  **-{bet:,} ₡**",                    inline=False)

    # Symbol guide
    guide = "  ".join(f"{s[0]}=×{s[2]}" for s in SLOT_SYMBOLS)
    e2.set_footer(text=f"Payouts: {guide}")
    await msg.edit(embed=e2)

# ── Blackjack ─────────────────────────────────────────────────────────────────
@bot.command(name="blackjack", aliases=["bj","21"])
async def blackjack_cmd(ctx, amount: str):
    """Play blackjack against the dealer. -bj <amount>"""
    if ctx.author.id in _bj_sessions:
        await ctx.send("❌ You already have an active blackjack game! Finish it first."); return
    bet, err = _bet_check(ctx, amount)
    if err: await ctx.send(err); return
    if not db_deduct_credits(ctx.author.id, bet):
        await ctx.send("❌ Could not deduct credits."); return

    deck = new_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    sess = {"player": player, "dealer": dealer, "deck": deck, "bet": bet, "bj": False}

    # Check natural blackjack
    if hand_value(player) == 21:
        sess["bj"] = True
        payout = int(bet * 2.5)
        db_add_credits(ctx.author.id, payout)
        db_record_casino(ctx.author.id, bet, payout - bet)
        e = _bj_embed(sess, result="blackjack")
        await ctx.send(embed=e); return

    _bj_sessions[ctx.author.id] = sess
    e = _bj_embed(sess)
    await ctx.send(embed=e, view=BlackjackView(ctx.author.id))

# ── Roulette ──────────────────────────────────────────────────────────────────
@bot.command(name="roulette", aliases=["rl","rlt"])
async def roulette_cmd(ctx, amount: str, *, bet_type: str = "red"):
    """Bet on roulette. -roulette <amount> <red|black|even|odd|low|high|0-36>"""
    bet, err = _bet_check(ctx, amount)
    if err: await ctx.send(err); return
    bt = bet_type.lower().strip()
    valid = {"red","black","even","odd","low","high"} | {str(n) for n in range(37)}
    if bt not in valid:
        await ctx.send("❌ Valid bets: `red` `black` `even` `odd` `low` `high` or a number `0`–`36`."); return
    if not db_deduct_credits(ctx.author.id, bet):
        await ctx.send("❌ Could not deduct credits."); return

    number = random.randint(0, 36)
    reds = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    color_str = "🟥 Red" if number in reds else ("⬛ Black" if number > 0 else "🟩 Zero")
    mult = roulette_resolve(bt, number)
    payout = bet * mult
    net = payout - bet
    if payout: db_add_credits(ctx.author.id, payout)
    db_record_casino(ctx.author.id, bet, net)

    color = 0x2ECC71 if net > 0 else (0x8B9099 if net == 0 else 0xFF4444)
    e = discord.Embed(title="🎡  Roulette", color=color)
    e.add_field(name="Ball lands on", value=f"**{number}**  {color_str}", inline=True)
    e.add_field(name="Your bet",      value=f"**{bt.capitalize()}**",     inline=True)
    e.add_field(name="Wagered",       value=f"**{bet:,} ₡**",             inline=True)
    if net > 0:
        e.add_field(name="Outcome", value=f"✅  **WIN!**  +{net:,} ₡  (×{mult})", inline=False)
    elif net == 0:
        e.add_field(name="Outcome", value=f"🤝  **PUSH**  Bet returned", inline=False)
    else:
        e.add_field(name="Outcome", value=f"❌  **LOSE!**  -{bet:,} ₡", inline=False)
    e.set_footer(
        text="Payouts: color/even-odd/low-high=×2 · exact number=×35",
        )
    await ctx.send(embed=e)

# ── Dice ──────────────────────────────────────────────────────────────────────
@bot.command(name="dice", aliases=["roll","dr"])
async def dice_cmd(ctx, amount: str, guess: int = None):
    """
    Roll two dice. -dice <amount> [guess 2-12]
    Exact guess pays ×6. High (7+) or Low (≤6) pays ×1.8.
    If no guess: high/low auto selected.
    """
    bet, err = _bet_check(ctx, amount)
    if err: await ctx.send(err); return
    if guess is not None and not (2 <= guess <= 12):
        await ctx.send("❌ Guess must be between 2 and 12."); return
    if not db_deduct_credits(ctx.author.id, bet):
        await ctx.send("❌ Could not deduct credits."); return

    d1, d2 = random.randint(1,6), random.randint(1,6)
    total = d1 + d2
    dice_faces = ["⚀","⚁","⚂","⚃","⚄","⚅"]

    if guess is not None:
        won = total == guess
        mult = 6
        outcome_str = f"Exact guess **{guess}**"
    else:
        # Auto: player guesses high (7+) if they didn't provide guess
        player_high = total >= 7
        won = player_high  # by default player wins on high
        mult = 2
        outcome_str = "High (7+) auto-bet"

    payout = int(bet * mult) if won else 0
    net = payout - bet
    if payout: db_add_credits(ctx.author.id, payout)
    db_record_casino(ctx.author.id, bet, net)

    color = 0x2ECC71 if won else 0xFF4444
    e = discord.Embed(title="🎲  Dice Roll", color=color)
    e.add_field(name="Roll",   value=f"{dice_faces[d1-1]} {dice_faces[d2-1]}  =  **{total}**", inline=True)
    e.add_field(name="Bet",    value=f"**{bet:,} ₡**",                                          inline=True)
    e.add_field(name="Type",   value=outcome_str,                                                inline=True)
    if won:
        e.add_field(name="Outcome", value=f"✅  **WIN!**  +{net:,} ₡  (×{mult})", inline=False)
    else:
        e.add_field(name="Outcome", value=f"❌  **LOSE!**  -{bet:,} ₡",            inline=False)
    e.set_footer(text="-dice <amount> <2-12> to guess exact (×6 payout)  ·  Default: high/low (×2)")
    await ctx.send(embed=e)

# ── Higher or Lower ───────────────────────────────────────────────────────────
@bot.command(name="highlow", aliases=["hl","hilo"])
async def highlow_cmd(ctx, amount: str, guess: str = "higher"):
    """
    Draw a card, guess if next is higher or lower. -highlow <amount> [higher|lower]
    Pays ×1.9. Tie = push.
    """
    bet, err = _bet_check(ctx, amount)
    if err: await ctx.send(err); return
    guess = guess.lower()
    if guess not in ("higher","lower","h","l","hi","lo"):
        await ctx.send("❌ Choose `higher` or `lower`."); return
    guess = "higher" if guess in ("higher","h","hi") else "lower"
    if not db_deduct_credits(ctx.author.id, bet):
        await ctx.send("❌ Could not deduct credits."); return

    ranks = [2,3,4,5,6,7,8,9,10,11,12,13,14]  # 11=J 12=Q 13=K 14=A
    rank_names = {11:"J",12:"Q",13:"K",14:"A"}
    def rank_name(r): return rank_names.get(r, str(r))
    suits = ["♠","♥","♦","♣"]

    c1 = random.choice(ranks); s1 = random.choice(suits)
    c2 = random.choice(ranks); s2 = random.choice(suits)

    if c1 == c2:   result = "push"
    elif guess == "higher" and c2 > c1: result = "win"
    elif guess == "lower"  and c2 < c1: result = "win"
    else: result = "lose"

    payout = int(bet * 1.9) if result == "win" else (bet if result == "push" else 0)
    net = payout - bet
    if payout: db_add_credits(ctx.author.id, payout)
    db_record_casino(ctx.author.id, bet, net)

    color = 0x2ECC71 if result=="win" else (0x8B9099 if result=="push" else 0xFF4444)
    e = discord.Embed(title="📈  Higher or Lower", color=color)
    e.add_field(name="First card",  value=f"`{rank_name(c1)}{s1}`", inline=True)
    e.add_field(name="Your guess",  value=f"**{guess.capitalize()}**", inline=True)
    e.add_field(name="Second card", value=f"`{rank_name(c2)}{s2}`", inline=True)
    if result == "win":
        e.add_field(name="Outcome", value=f"✅  **WIN!**  +{net:,} ₡", inline=False)
    elif result == "push":
        e.add_field(name="Outcome", value=f"🤝  **TIE!**  Bet returned", inline=False)
    else:
        e.add_field(name="Outcome", value=f"❌  **LOSE!**  -{bet:,} ₡", inline=False)
    e.set_footer(text="Pays ×1.9 on win  ·  Tie returns your bet")
    await ctx.send(embed=e)

# ── Rock Paper Scissors ───────────────────────────────────────────────────────
@bot.command(name="rps")
async def rps_cmd(ctx, amount: str, choice: str = "rock"):
    """Rock, paper, scissors vs the bot. -rps <amount> [rock|paper|scissors]"""
    bet, err = _bet_check(ctx, amount)
    if err: await ctx.send(err); return
    choice = choice.lower()
    aliases_map = {"r":"rock","p":"paper","s":"scissors","sc":"scissors"}
    choice = aliases_map.get(choice, choice)
    if choice not in ("rock","paper","scissors"):
        await ctx.send("❌ Choose `rock`, `paper`, or `scissors`."); return
    if not db_deduct_credits(ctx.author.id, bet):
        await ctx.send("❌ Could not deduct credits."); return

    bot_choice = random.choice(["rock","paper","scissors"])
    emojis = {"rock":"🪨","paper":"📄","scissors":"✂️"}
    beats = {"rock":"scissors","paper":"rock","scissors":"paper"}

    if choice == bot_choice:
        result = "push"
    elif beats[choice] == bot_choice:
        result = "win"
    else:
        result = "lose"

    payout = bet * 2 if result == "win" else (bet if result == "push" else 0)
    net = payout - bet
    if payout: db_add_credits(ctx.author.id, payout)
    db_record_casino(ctx.author.id, bet, net)

    color = 0x2ECC71 if result=="win" else (0x8B9099 if result=="push" else 0xFF4444)
    e = discord.Embed(title="✂️  Rock Paper Scissors", color=color)
    e.add_field(name="You",     value=f"{emojis[choice]} **{choice.capitalize()}**",     inline=True)
    e.add_field(name="Bot",     value=f"{emojis[bot_choice]} **{bot_choice.capitalize()}**", inline=True)
    e.add_field(name="Bet",     value=f"**{bet:,} ₡**",                                  inline=True)
    if result == "win":
        e.add_field(name="Outcome", value=f"✅  **WIN!**  +{bet:,} ₡", inline=False)
    elif result == "push":
        e.add_field(name="Outcome", value=f"🤝  **TIE!**  Bet returned", inline=False)
    else:
        e.add_field(name="Outcome", value=f"❌  **LOSE!**  -{bet:,} ₡", inline=False)
    await ctx.send(embed=e)

# ── Number Guess ──────────────────────────────────────────────────────────────
@bot.command(name="guess", aliases=["numguess","ng"])
async def guess_cmd(ctx, amount: str, number: int = None):
    """
    Guess a number 1–10. -guess <amount> <1-10>
    Correct = ×8 payout. One off = ×1.5.
    """
    bet, err = _bet_check(ctx, amount)
    if err: await ctx.send(err); return
    if number is None or not (1 <= number <= 10):
        await ctx.send("❌ Guess a number between 1 and 10."); return
    if not db_deduct_credits(ctx.author.id, bet):
        await ctx.send("❌ Could not deduct credits."); return

    answer = random.randint(1, 10)
    diff = abs(number - answer)

    if diff == 0:
        payout = bet * 8; result = "exact"; net = payout - bet
    elif diff == 1:
        payout = int(bet * 1.5); result = "close"; net = payout - bet
    else:
        payout = 0; result = "miss"; net = -bet

    if payout: db_add_credits(ctx.author.id, payout)
    db_record_casino(ctx.author.id, bet, net)

    color = 0xF1C40F if result=="exact" else (0x5EC4B8 if result=="close" else 0xFF4444)
    e = discord.Embed(title="🔢  Number Guess", color=color)
    e.add_field(name="Your guess", value=f"**{number}**",  inline=True)
    e.add_field(name="Answer",     value=f"**{answer}**",  inline=True)
    e.add_field(name="Bet",        value=f"**{bet:,} ₡**", inline=True)
    if result == "exact":
        e.add_field(name="Outcome", value=f"🎯  **EXACT!**  +{net:,} ₡  (×8)", inline=False)
    elif result == "close":
        e.add_field(name="Outcome", value=f"🔥  **ONE OFF!**  +{net:,} ₡  (×1.5)", inline=False)
    else:
        e.add_field(name="Outcome", value=f"❌  **MISS!**  -{bet:,} ₡", inline=False)
    e.set_footer(text="Exact = ×8  ·  One off = ×1.5  ·  Otherwise lose")
    await ctx.send(embed=e)

# ── Casino Stats ──────────────────────────────────────────────────────────────
@bot.command(name="casinostats", aliases=["cs","mygamble"])
async def casinostats_cmd(ctx, member: discord.Member = None):
    """View your casino statistics."""
    target = member or ctx.author
    s = db_get_casino_stats(target.id)
    net = int(s["total_won"]) - int(s["total_lost"])
    sign = "+" if net >= 0 else ""
    color = 0x2ECC71 if net >= 0 else 0xFF4444
    e = discord.Embed(title=f"🎰  {target.display_name}'s Casino Stats", color=color)
    e.set_thumbnail(url=target.display_avatar.url)
    e.add_field(name="🎮 Games Played", value=f"**{int(s['games_played']):,}**",      inline=True)
    e.add_field(name="💸 Total Wagered", value=f"**{int(s['total_bet']):,} ₡**",      inline=True)
    e.add_field(name="📊 Net P&L",       value=f"**{sign}{net:,} ₡**",                inline=True)
    e.add_field(name="✅ Won",           value=f"**{int(s['total_won']):,} ₡**",      inline=True)
    e.add_field(name="❌ Lost",          value=f"**{int(s['total_lost']):,} ₡**",     inline=True)
    e.add_field(name="🏆 Biggest Win",   value=f"**{int(s['biggest_win']):,} ₡**",   inline=True)
    if int(s['games_played']) > 0:
        roi = round((net / int(s['total_bet'])) * 100, 1) if s['total_bet'] else 0
        e.add_field(name="📈 ROI", value=f"**{'+' if roi>=0 else ''}{roi}%**", inline=True)
    e.set_footer(text="-casinostats  ·  Use -lb casino for leaderboard")
    await ctx.send(embed=e)

# ─── Casino Hub ───────────────────────────────────────────────────────────────
@bot.command(name="casino", aliases=["games","gamble"])
async def casino_cmd(ctx):
    """Show all available casino games."""
    e = discord.Embed(title="🎰  Casino", color=CASINO_COLOR)
    e.description = (f"Bet range: **{MIN_BET:,}** – **{MAX_BET:,}** ₡\n"
                     f"Use `all`, `half`, `10k`, `500k` as shortcuts.\n\u200b")
    e.add_field(name="🪙  Coin Flip",        value="`-coinflip <amount> [heads|tails]`\nBet on heads or tails.  Pays ×2", inline=False)
    e.add_field(name="🎰  Slots",            value="`-slots <amount>`\nSpin three reels.  Up to ×100 jackpot!", inline=False)
    e.add_field(name="🃏  Blackjack",        value="`-bj <amount>`\nBeat the dealer to 21.  Hit, Stand or Double Down.", inline=False)
    e.add_field(name="🎡  Roulette",         value="`-roulette <amount> <red|black|even|odd|low|high|0-36>`\nSingle number pays ×35!", inline=False)
    e.add_field(name="🎲  Dice",             value="`-dice <amount> [2-12]`\nRoll two dice.  Exact guess = ×6.", inline=False)
    e.add_field(name="📈  Higher or Lower",  value="`-highlow <amount> [higher|lower]`\nGuess the next card.  Pays ×1.9.", inline=False)
    e.add_field(name="✂️  Rock Paper Scissors", value="`-rps <amount> [rock|paper|scissors]`\nBeat the bot.  Pays ×2.", inline=False)
    e.add_field(name="🔢  Number Guess",     value="`-guess <amount> <1-10>`\nExact = ×8  ·  One off = ×1.5.", inline=False)
    e.add_field(name="📊  Stats",            value="`-casinostats` — Your win/loss record\n`-lb casino` — Casino leaderboard", inline=False)
    e.set_footer(text="Good luck! 🍀  Credits are earned by chatting.")
    await ctx.send(embed=e)

# ═══════════════════════════════════════════════════════════════════════════════
# ─── CUSTOM REPLY COMMANDS ────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="addreply", aliases=["ar"])
async def addreply_cmd(ctx, target: str, match_type: str, trigger: str, *, reply: str):
    """
    [Admin] Add a custom auto-reply.
    -addreply <@user|everyone> <contains|exact|startswith> <trigger> <reply>

    Examples:
      -addreply @user123 contains hello  Hello there!
      -addreply everyone exact !hi       Hi everyone!
    """
    if not is_admin(ctx.author): await ctx.send("❌ No permission."); return
    match_type = match_type.lower()
    if match_type not in ("contains","exact","startswith"):
        await ctx.send("❌ match_type must be `contains`, `exact`, or `startswith`."); return

    uid = None
    if target.lower() != "everyone":
        try:
            member = await commands.MemberConverter().convert(ctx, target)
            uid = member.id
        except Exception:
            await ctx.send("❌ Could not find that user. Use @mention or `everyone`."); return

    db_add_custom_reply(uid, trigger, reply, match_type)
    scope = f"<@{uid}>" if uid else "**everyone**"
    e = discord.Embed(title="✅  Custom Reply Added", color=0x2ECC71)
    e.add_field(name="Scope",      value=scope,                     inline=True)
    e.add_field(name="Match",      value=f"`{match_type}`",         inline=True)
    e.add_field(name="Trigger",    value=f"`{trigger}`",            inline=True)
    e.add_field(name="Reply",      value=reply[:500],               inline=False)
    await ctx.send(embed=e)

@bot.command(name="listreplies", aliases=["lsr","replies"])
async def listreplies_cmd(ctx):
    """[Admin] List all custom replies."""
    if not is_admin(ctx.author): await ctx.send("❌ No permission."); return
    rows = db_get_custom_replies()
    if not rows: await ctx.send("No custom replies set."); return
    e = discord.Embed(title="Custom Replies", color=0x5865F2)
    e.description = f"{len(rows)} rule(s) active\n\u200b"
    for row in rows[:20]:
        scope = f"<@{row['trigger_uid']}>" if row["trigger_uid"] else "everyone"
        e.add_field(
            name=f"ID `{row['id']}`  ·  {scope}",
            value=(f"**Match:** `{row['match_type']}`  **Trigger:** `{row['trigger_kw'] or '(any)'}`\n"
                   f"**Reply:** {row['reply'][:120]}"),
            inline=False)
    await ctx.send(embed=e)

@bot.command(name="delreply", aliases=["dr2","removereply"])
async def delreply_cmd(ctx, reply_id: int):
    """[Admin] Delete a custom reply by ID. -delreply <id>"""
    if not is_admin(ctx.author): await ctx.send("❌ No permission."); return
    db_delete_custom_reply(reply_id)
    await ctx.send(f"🗑️ Deleted reply ID `{reply_id}`.")

# ═══════════════════════════════════════════════════════════════════════════════
# ─── EXISTING COMMANDS (unchanged) ───────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="balance", aliases=["bal","credits","wallet"])
async def balance_cmd(ctx, member: discord.Member = None):
    target = member or ctx.author
    w = db_get_wallet(target.id)
    effects = db_get_effects(target.id)
    e = discord.Embed(title=f"{target.display_name}'s Wallet", color=0xF1C40F)
    e.set_thumbnail(url=target.display_avatar.url)
    e.add_field(name="👛 Wallet",  value=f"**{w['balance']:,} ₡**", inline=True)
    e.add_field(name="🏦 Bank",   value=f"**{w['bank']:,} ₡**",    inline=True)
    e.add_field(name="💰 Total",  value=f"**{w['balance']+w['bank']:,} ₡**", inline=True)
    e.add_field(name="📈 Interest", value="+1% per message on bank balance", inline=False)
    if effects:
        eff_lines = []
        for etype, row in effects.items():
            if etype == "luck_bonus":   eff_lines.append(f"🍀 Luck +{int(row['value']):,}  ·  {int(row['remaining'])} msg left")
            elif etype == "stack_mult": eff_lines.append(f"⚡ ×{int(row['value'])} rolls  ·  {int(row['remaining'])} msg left")
            elif etype == "skip_msgs":  eff_lines.append(f"⏩ Skipping  ·  {int(row['remaining'])} msgs left")
        if eff_lines: e.add_field(name="Active Effects", value="\n".join(eff_lines), inline=False)
    e.set_footer(text="Earn credits by chatting  ·  Milestones: ×2 @1k, ×3 @10k, ×4 @25k, ×5 @50k, ×10 @100k msgs")
    await ctx.send(embed=e)

@bot.command(name="bank", aliases=["savings"])
async def bank_cmd(ctx, member: discord.Member = None):
    target = member or ctx.author
    w = db_get_wallet(target.id)
    e = discord.Embed(title=f"🏦  {target.display_name}'s Bank", color=0x2ECC71)
    e.set_thumbnail(url=target.display_avatar.url)
    e.add_field(name="🏦 Bank Balance", value=f"**{w['bank']:,} ₡**", inline=True)
    e.add_field(name="👛 Wallet",       value=f"**{w['balance']:,} ₡**", inline=True)
    e.add_field(name="💰 Total",        value=f"**{w['balance']+w['bank']:,} ₡**", inline=True)
    e.add_field(name="📈 Interest Rate", value="**+1%** of bank balance per message sent", inline=False)
    e.set_footer(text="Use -deposit <amount|all> to move credits into the bank")
    await ctx.send(embed=e)

@bot.command(name="deposit", aliases=["dep"])
async def deposit_cmd(ctx, amount: str):
    w = db_get_wallet(ctx.author.id)
    if amount.lower() == "all": amount = str(w["balance"])
    try: amt = int(amount)
    except ValueError: await ctx.send("❌ Invalid amount."); return
    if amt <= 0: await ctx.send("❌ Amount must be positive."); return
    ok, err = db_transfer_to_bank(ctx.author.id, amt)
    if not ok: await ctx.send(f"❌ {err}"); return
    await ctx.send(f"🏦 Deposited **{amt:,} ₡** into your bank.")

@bot.command(name="withdraw", aliases=["with"])
async def withdraw_cmd(ctx, amount: str):
    w = db_get_wallet(ctx.author.id)
    if amount.lower() == "all": amount = str(w["bank"])
    try: amt = int(amount)
    except ValueError: await ctx.send("❌ Invalid amount."); return
    if amt <= 0: await ctx.send("❌ Amount must be positive."); return
    ok, err = db_transfer_from_bank(ctx.author.id, amt)
    if not ok: await ctx.send(f"❌ {err}"); return
    await ctx.send(f"👛 Withdrew **{amt:,} ₡** to your wallet.")

@bot.command(name="items", aliases=["bag","backpack"])
async def items_cmd(ctx, member: discord.Member = None):
    target = member or ctx.author
    rows = db_get_item_inventory(target.id)
    effects = db_get_effects(target.id)
    e = discord.Embed(title=f"{target.display_name}'s Items", color=0x9B59B6)
    if not rows:
        e.description = "No items yet. Check the `-store` for today's offerings!"
    else:
        for row in rows:
            item = ITEMS.get(row["item_id"])
            if not item: continue
            tier = item["tier"]
            e.add_field(name=f"{ITEM_TIER_EMOJI[tier]}  {item['name']}  ×{row['quantity']}",
                value=f"`{ITEM_TIER_BADGE[tier]}`  ·  {item['desc']}", inline=False)
    if effects:
        eff_lines = []
        for etype, row in effects.items():
            if etype == "luck_bonus":   eff_lines.append(f"🍀 **Luck Bonus** +{int(row['value']):,}  ·  {int(row['remaining'])} message(s) left")
            elif etype == "stack_mult": eff_lines.append(f"⚡ **Stack ×{int(row['value'])}**  ·  {int(row['remaining'])} message(s) left")
            elif etype == "skip_msgs":  eff_lines.append(f"⏩ **Skip** active  ·  {int(row['remaining'])} messages left to skip")
        e.add_field(name="⚗️ Active Effects", value="\n".join(eff_lines), inline=False)
    await ctx.send(embed=e)

@bot.command(name="use")
async def use_cmd(ctx, *, item_name: str):
    match = next((iid for iid, item in ITEMS.items() if item["name"].lower() == item_name.lower()), None)
    if not match:
        match = next((iid for iid, item in ITEMS.items() if item_name.lower() in item["name"].lower()), None)
    if not match:
        await ctx.send(f"❌ Unknown item `{item_name}`. Use `-items` to see what you own."); return
    ok = db_use_item(ctx.author.id, match)
    if not ok:
        await ctx.send(f"❌ You don't own **{ITEMS[match]['name']}**."); return
    item = ITEMS[match]
    effect = item["effect"]
    if effect == "skip_msgs":
        db_apply_effect(ctx.author.id, "skip_msgs", item["value"], item["value"])
        e = discord.Embed(title=f"⏩  {item['name']} activated!", color=ITEM_TIER_COLOR[item['tier']])
        e.description = f"Skipping the next **{item['value']:,}** messages (counts toward streaks & credits)."
    else:
        db_apply_effect(ctx.author.id, effect, item["value"], item["duration"])
        e = discord.Embed(title=f"✅  {item['name']} activated!", color=ITEM_TIER_COLOR[item['tier']])
        e.description = item["desc"]
        if effect == "luck_bonus":
            e.add_field(name="Effect",   value=f"+{item['value']:,} luck", inline=True)
            e.add_field(name="Duration", value=f"{item['duration']} message", inline=True)
        elif effect == "stack_mult":
            e.add_field(name="Multiplier", value=f"×{item['value']} rolls", inline=True)
            e.add_field(name="Duration",   value=f"{item['duration']:,} messages", inline=True)
    e.set_footer(text="Effects stack — use multiple potions to combine bonuses!")
    await ctx.send(embed=e)

@bot.command(name="store", aliases=["shop","dailystore"])
async def store_cmd(ctx):
    rows = db_get_daily_store()
    if not rows: rows = db_rotate_store()
    await ctx.send(embed=build_daily_store_embed(rows), view=DailyStoreView(rows, ctx.author.id))

@bot.command(name="buy")
async def buy_cmd(ctx, slot: int):
    slot_idx = slot - 1
    db_ensure_wallet(ctx.author.id)
    try:
        item_id, price = db_buy_store_item(slot_idx, ctx.author.id)
    except ValueError as err:
        await ctx.send(f"❌ {err}"); return
    item = ITEMS.get(item_id)
    db_add_item(ctx.author.id, item_id, 1)
    tier = item["tier"]
    e = discord.Embed(title=f"{ITEM_TIER_EMOJI[tier]}  Purchased: {item['name']}", color=ITEM_TIER_COLOR[tier])
    e.add_field(name="Paid",   value=f"**{price:,} ₡**",          inline=True)
    e.add_field(name="Tier",   value=f"`{ITEM_TIER_BADGE[tier]}`", inline=True)
    e.add_field(name="Effect", value=item["desc"],                 inline=False)
    e.set_footer(text="Use -use <item name> to activate it")
    await ctx.send(embed=e)

@bot.command(name="give")
async def give_cmd(ctx, target: str, kind: str, *, amount_or_item: str):
    if not is_admin(ctx.author): await ctx.send("❌ No permission."); return
    targets = []
    if target.lower() == "everyone":
        targets = [m for m in ctx.guild.members if not m.bot]
    else:
        try:
            member = await commands.MemberConverter().convert(ctx, target)
            targets = [member]
        except Exception:
            await ctx.send("❌ Could not find that user."); return
    kind = kind.lower()
    if kind == "credits":
        try: amt = int(amount_or_item)
        except ValueError: await ctx.send("❌ Amount must be a number."); return
        for m in targets:
            db_ensure_wallet(m.id)
            db_add_credits(m.id, amt)
        noun = "everyone" if len(targets) > 1 else targets[0].display_name
        await ctx.send(f"✅ Gave **{amt:,} ₡** to **{noun}** ({len(targets)} user(s)).")
    elif kind == "item":
        item_name = amount_or_item.strip()
        match = next((iid for iid, item in ITEMS.items() if item["name"].lower() == item_name.lower()), None)
        if not match:
            match = next((iid for iid, item in ITEMS.items() if item_name.lower() in item["name"].lower()), None)
        if not match:
            names = ", ".join(f"`{v['name']}`" for v in ITEMS.values())
            await ctx.send(f"❌ Unknown item. Available: {names}"); return
        for m in targets:
            db_add_item(m.id, match, 1)
        noun = "everyone" if len(targets) > 1 else targets[0].display_name
        await ctx.send(f"✅ Gave **{ITEMS[match]['name']}** to **{noun}** ({len(targets)} user(s)).")
    else:
        await ctx.send("❌ Kind must be `credits` or `item`.")

@bot.command(name="admingive", aliases=["ag"])
async def admingive_cmd(ctx, member: discord.Member, kind: str, *, value: str):
    if not is_admin(ctx.author): await ctx.send("❌ No permission."); return
    await give_cmd(ctx, member.mention, kind, amount_or_item=value)

@bot.command(name="rerollstore", aliases=["reroll"])
async def reroll_store_cmd(ctx):
    if not is_admin(ctx.author): await ctx.send("❌ No permission."); return
    rows = db_rotate_store()
    e = build_daily_store_embed(rows)
    e.title = "🛒  Store Manually Rerolled"
    await ctx.send(embed=e)

@bot.command(name="prizemaker", aliases=["pm"])
async def prizemaker(ctx):
    if not is_admin(ctx.author): await ctx.send("❌ You don't have permission to use this."); return
    e = discord.Embed(title="Prize Manager", color=0x5865F2)
    e.description = "Manage prizes from the buttons below.\n\u200b"
    e.add_field(name="Rarity tiers", value=(
        "⚪  `· COMMON`     — odds up to 1/9,999\n"
        "🟠  `○ RARE`       — 1/10,000+\n"
        "🩵  `○ RARE+`      — 1/100,000+\n"
        "💜  `◇ EPIC`       — 1/1,000,000+\n"
        "💙  `◇ EPIC+`      — 1/10,000,000+\n"
        "💎  `◆ LEGENDARY`  — 1/100,000,000+  *(pings @everyone)*\n"
        "🌑  `◈ MYTHIC`     — 1/1,000,000,000+  *(pings @everyone + announcement)*"), inline=False)
    e.set_footer(text="Admin only  ·  All data persists in Supabase")
    await ctx.send(embed=e, view=PrizeMakerView())

@bot.command(name="collection", aliases=["col","c"])
async def collection_cmd(ctx):
    prizes = db_load_prizes()
    if not prizes: await ctx.send("No prizes exist yet. Admins can add some with `-prizemaker`."); return
    disc = {d["prize_name"]: d for d in db_get_collection()}
    v = CollectionView(list(prizes), disc, owner_id=ctx.author.id)
    await ctx.send(embed=v.build_embed(), view=v)

@bot.command(name="inventory", aliases=["inv","i"])
async def inventory_cmd(ctx, member: discord.Member = None, sort: str = "rarity"):
    target = member or ctx.author
    if sort not in INV_SORT_OPTIONS: sort = "rarity"
    entries = db_get_inventory_sorted(target.id, sort)
    if not entries:
        noun = "You have" if target == ctx.author else f"**{target.display_name}** has"
        await ctx.send(f"{noun} no prizes yet — keep chatting to earn one!"); return
    v = InventoryView(list(entries), target, sort=sort, owner_id=ctx.author.id)
    await ctx.send(embed=v.build_embed(), view=v)

@bot.command(name="search", aliases=["find","s"])
async def search_cmd(ctx, *, query: str):
    results = db_search_inventory(ctx.author.id, query)
    if not results: await ctx.send(f"No prizes matching `{query}` in your inventory."); return
    e = discord.Embed(title=f"Search: {query}", color=0x5865F2)
    e.description = f"{len(results[:10])} result(s)\n\u200b"
    for entry in list(results)[:10]:
        chance = entry.get("chance")
        _, _, _, _, tier = get_rarity_info(chance) if chance else (*([None]*4),"common")
        disc = db_get_disc(entry["prize_name"])
        e.add_field(name=f"{RARITY_EMOJI[tier]}  {entry['prize_name']}",
            value=(f"`{TIER_BADGE[tier]}`  ·  1/{chance:,}\nYou own  **{entry['quantity']}×**\n"
                   f"First found  {fmt_dt_s(entry.get('first_found_at'))}\n"
                   f"Server total  {disc.get('total_found',0) if disc else 0}×"), inline=False)
    await ctx.send(embed=e)

@bot.command(name="leaderboard", aliases=["lb","top"])
async def leaderboard_cmd(ctx, lb_type: str = "collected"):
    if lb_type not in ("collected","rarest","messages","credits","casino"): lb_type = "collected"
    v = LeaderboardView(ctx.guild, lb_type=lb_type, owner_id=ctx.author.id)
    await ctx.send(embed=v.build_embed(), view=v)

@bot.command(name="rarest", aliases=["rare"])
async def rarest_cmd(ctx):
    results = db_rarest()
    if not results: await ctx.send("No prizes have been discovered yet."); return
    e = discord.Embed(title="Rarest finds", color=0x5865F2)
    e.description = "The hardest prizes to roll that have been found here.\n\u200b"
    for r in results:
        _, _, _, _, tier = get_rarity_info(r["chance"])
        e.add_field(name=f"{RARITY_EMOJI[tier]}  {r['name']}",
            value=(f"`{TIER_BADGE[tier]}`  ·  1/{r['chance']:,}\n"
                   f"First by **{r.get('first_user','—')}**  ·  {fmt_dt_s(r.get('first_at'))}\n"
                   f"{r.get('total_found',0)}× found total"), inline=True)
    await ctx.send(embed=e)

@bot.command(name="profile", aliases=["p","prof"])
async def profile_cmd(ctx, member: discord.Member = None):
    target = member or ctx.author; is_self = (target == ctx.author)
    profile = db_get_profile(target.id); summary = db_user_inventory_summary(target.id)
    tp = len(get_prizes_cached())
    unique = int(summary.get("unique_prizes",0) or 0)
    total  = int(summary.get("total_found",0) or 0)
    comp = round(unique/tp*100,1) if tp else 0
    bar  = "▰"*int(comp/10) + "▱"*(10-int(comp/10))
    w = db_get_wallet(target.id)
    e = discord.Embed(title=target.display_name, color=0x5865F2)
    e.set_thumbnail(url=target.display_avatar.url)
    bio = (profile.get("bio") if profile else None) or "*No bio — use `-setbio` to add one.*"
    e.add_field(name="Bio", value=bio, inline=False)
    for key, label in [("equipped_prize","Equipped prize"),("showcase_prize","Showcase prize")]:
        pname = profile.get(key) if profile else None
        if pname:
            p = db_get_prize(pname)
            if p:
                _, _, _, _, tier = get_rarity_info(p["chance"])
                e.add_field(name=label, value=f"{RARITY_EMOJI[tier]}  **{pname}**  `{TIER_BADGE[tier]}`", inline=False)
                if key == "showcase_prize" and p.get("image"): e.set_image(url=p["image"])
    e.add_field(name="Unique",   value=f"**{unique}**",  inline=True)
    e.add_field(name="Total",    value=f"**{total}**",   inline=True)
    e.add_field(name="Messages", value=f"**{profile.get('total_messages',0) if profile else 0}**", inline=True)
    e.add_field(name="👛 Wallet", value=f"**{w['balance']:,} ₡**", inline=True)
    e.add_field(name="🏦 Bank",  value=f"**{w['bank']:,} ₡**",    inline=True)
    e.add_field(name="Collection progress", value=f"`{bar}`  {comp}%  ({unique}/{tp})", inline=False)
    best = summary.get("best_prize")
    if best:
        _, _, _, _, tier = get_rarity_info(best["chance"])
        e.add_field(name="Best prize", value=f"{RARITY_EMOJI[tier]}  **{best['prize_name']}**  `{TIER_BADGE[tier]}`", inline=False)
    ff = summary.get("first_find")
    if ff: e.set_footer(text=f"First prize found  {fmt_dt(ff)}")
    await ctx.send(embed=e, view=ProfileView(target, is_self, owner_id=ctx.author.id))

@bot.command(name="setbio")
async def setbio_cmd(ctx, *, bio: str = ""):
    db_upsert_profile(ctx.author.id, bio=bio.strip() or None)
    await ctx.send("✅ Bio updated." if bio.strip() else "✅ Bio cleared.", delete_after=5)

@bot.command(name="showcase")
async def showcase_cmd(ctx):
    entries = db_get_inventory(ctx.author.id)
    if not entries: await ctx.send("You have no prizes to showcase yet."); return
    await ctx.send("Choose your showcase prize:", view=_make_view(ShowcaseSelect, list(entries)))

@bot.command(name="equip")
async def equip_cmd(ctx, *, prize_name: str):
    results = db_search_inventory(ctx.author.id, prize_name)
    exact = next((r for r in results if r["prize_name"].lower() == prize_name.lower()), results[0] if results else None)
    if not exact: await ctx.send(f"You don't own a prize matching `{prize_name}`.\nUse `-inv` to see your prizes."); return
    name = exact["prize_name"]; chance = exact.get("chance",1)
    _, color, _, _, tier = get_rarity_info(chance)
    new_nick = f"[{name}] {ctx.author.name}"[:32]
    try:
        await ctx.author.edit(nick=new_nick)
        db_upsert_profile(ctx.author.id, equipped_prize=name)
        e = discord.Embed(title="Prize equipped", description=f"Your nickname is now **{new_nick}**", color=color)
        e.add_field(name="Prize", value=f"{RARITY_EMOJI[tier]}  **{name}**", inline=True)
        e.add_field(name="Tier",  value=f"`{TIER_BADGE[tier]}`",             inline=True)
        e.set_footer(text="Use -unequip to remove it")
        await ctx.send(embed=e)
    except discord.Forbidden: await ctx.send("I don't have permission to change your nickname.")
    except discord.HTTPException as ex: await ctx.send(f"Failed to set nickname: {ex}")

@bot.command(name="unequip")
async def unequip_cmd(ctx):
    try:
        await ctx.author.edit(nick=None)
        db_upsert_profile(ctx.author.id, equipped_prize=None)
        await ctx.send("✅ Prize unequipped — nickname restored.")
    except discord.Forbidden: await ctx.send("I don't have permission to change your nickname.")
    except discord.HTTPException as ex: await ctx.send(f"Failed: {ex}")

@bot.command(name="compare", aliases=["vs"])
async def compare_cmd(ctx, member: discord.Member):
    if member == ctx.author: await ctx.send("You can't compare with yourself."); return
    data = db_compare_inventories(ctx.author.id, member.id)
    e = discord.Embed(title=f"{ctx.author.display_name}  vs  {member.display_name}", color=0x5865F2)
    e.add_field(name=f"Both have  ({len(data['shared'])})",               value=", ".join(sorted(data["shared"]))[:1000]  or "*None*", inline=False)
    e.add_field(name=f"Only {ctx.author.display_name}  ({len(data['only_u1'])})", value=", ".join(sorted(data["only_u1"]))[:1000] or "*None*", inline=False)
    e.add_field(name=f"Only {member.display_name}  ({len(data['only_u2'])})",     value=", ".join(sorted(data["only_u2"]))[:1000] or "*None*", inline=False)
    await ctx.send(embed=e)

@bot.command(name="prizeinfo", aliases=["pi"])
async def prizeinfo_cmd(ctx, *, name: str):
    prize = db_get_prize(name)
    if not prize:
        matches = [p for p in db_load_prizes() if name.lower() in p["name"].lower()]
        if not matches: await ctx.send(f"No prize found matching `{name}`."); return
        if len(matches) > 1:
            names_str = "  ·  ".join(f"`{p['name']}`" for p in matches[:10])
            await ctx.send(f"Multiple matches: {names_str}\nBe more specific."); return
        prize = matches[0]
    disc = db_get_disc(prize["name"])
    _, color, _, _, tier = get_rarity_info(prize["chance"])
    e = discord.Embed(title=f"{RARITY_EMOJI[tier]}  {prize['name']}", color=color)
    e.add_field(name="Tier",     value=f"`{TIER_BADGE[tier]}`",            inline=True)
    e.add_field(name="Odds",     value=f"1 in {prize['chance']:,}",        inline=True)
    e.add_field(name="Spectrum", value=f"`{rarity_bar(prize['chance'])}`", inline=True)
    if prize.get("description"): e.add_field(name="About", value=f"*{prize['description']}*", inline=False)
    if disc:
        if disc.get("discovered"):
            e.add_field(name="Discovery", value=(f"First found by **{disc.get('first_user','—')}**\n"
                f"{fmt_dt(disc.get('first_at'))}\nFound **{disc.get('total_found',0)}×** total"), inline=False)
        else: e.add_field(name="Status", value="*Not yet discovered by anyone.*", inline=False)
    if prize.get("image"): e.set_image(url=prize["image"])
    await ctx.send(embed=e)

@bot.command(name="recent", aliases=["feed"])
async def recent_cmd(ctx):
    results = db_recent_finds(10)
    if not results: await ctx.send("No prizes found yet."); return
    e = discord.Embed(title="Recent finds", color=0x5865F2)
    e.description = "The last 10 prizes rolled on this server.\n\u200b"
    for r in results:
        m = ctx.guild.get_member(r["user_id"])
        uname = m.display_name if m else "User " + str(r["user_id"])
        _, _, _, _, tier = get_rarity_info(r["chance"])
        e.add_field(name=f"{RARITY_EMOJI[tier]}  {r['prize_name']}",
            value=f"`{TIER_BADGE[tier]}`  ·  {uname}  ·  {fmt_dt_s(r['last_found_at'])}", inline=False)
    await ctx.send(embed=e)

@bot.command(name="stats")
async def stats_cmd(ctx): await ctx.send(embed=build_stats_embed(db_stats()))

@bot.command(name="ping")
async def ping_cmd(ctx): await ctx.send(f"Pong!  `{round(bot.latency*1000)} ms`")

@bot.command(name="help", aliases=["h"])
async def help_cmd(ctx):
    e = discord.Embed(title="Commands", color=0x5865F2)
    e.description = "Prefix: `-`  ·  `[optional]`  `<required>`\n\u200b"

    e.add_field(name="🏆 Collection", value=(
        "`-collection` (`-col`) — Browse all prizes & discovery status\n"
        "`-rarest` — Show the rarest prizes ever found\n"
        "`-recent` (`-feed`) — Live feed of last 10 finds\n"
        "`-prizeinfo <name>` (`-pi`) — Detailed prize info\n"
        "`-stats` — Server statistics dashboard"
    ), inline=False)

    e.add_field(name="🎒 Inventory", value=(
        "`-inventory [sort]` (`-inv`) — Your collection. Sorts: `rarity` `quantity` `name` `recent` `oldest`\n"
        "`-search <name>` (`-find`) — Search your inventory\n"
        "`-compare <@user>` (`-vs`) — Compare prizes with someone"
    ), inline=False)

    e.add_field(name="👤 Profile", value=(
        "`-profile [@user]` (`-p`) — View a profile card\n"
        "`-setbio [text]` — Set your bio (leave blank to clear)\n"
        "`-showcase` — Pin a prize to your profile\n"
        "`-equip <prize>` — Wear a prize as a nickname tag\n"
        "`-unequip` — Remove equipped prize & restore nickname"
    ), inline=False)

    e.add_field(name="💰 Credits & Economy", value=(
        "`-balance` (`-bal`) — Wallet, bank & active effects\n"
        "`-bank` (`-savings`) — Detailed bank account view\n"
        "`-deposit <amount|all>` (`-dep`) — Deposit to bank (+1% interest/msg)\n"
        "`-withdraw <amount|all>` (`-with`) — Withdraw from bank"
    ), inline=False)

    e.add_field(name="🛒 Items", value=(
        "`-store` (`-shop`) — Daily item store (resets midnight UTC+7)\n"
        "`-buy <slot>` — Buy item by slot number\n"
        "`-items` (`-bag`) — Your items & active effects\n"
        "`-use <item name>` — Activate an item"
    ), inline=False)

    e.add_field(name="🎰 Casino", value=(
        "`-casino` — Show all games & rules\n"
        "`-coinflip <amt> [heads|tails]` (`-cf`) — ×2\n"
        "`-slots <amt>` (`-sl`) — up to ×100 jackpot\n"
        "`-blackjack <amt>` (`-bj`) — Beat the dealer\n"
        "`-roulette <amt> <bet>` (`-rl`) — up to ×35\n"
        "`-dice <amt> [2-12]` (`-dr`) — Exact guess ×6\n"
        "`-highlow <amt> [higher|lower]` (`-hl`) — ×1.9\n"
        "`-rps <amt> [rock|paper|scissors]` — ×2\n"
        "`-guess <amt> <1-10>` (`-ng`) — Exact ×8\n"
        "`-casinostats` (`-cs`) — Your casino record"
    ), inline=False)

    e.add_field(name="📊 Leaderboard & Misc", value=(
        "`-leaderboard [type]` (`-lb`) — Rankings: `collected` `rarest` `messages` `credits` `casino`\n"
        "`-ping` — Check bot latency"
    ), inline=False)

    if is_admin(ctx.author):
        e.add_field(name="⚙️ Admin", value=(
            "`-prizemaker` (`-pm`) — Open prize management panel\n"
            "`-give <@user|everyone> credits <amount>` — Give credits\n"
            "`-give <@user|everyone> item <name>` — Give an item\n"
            "`-rerollstore` — Force-reroll the daily store\n"
            "`-addreply <@user|everyone> <contains|exact|startswith> <trigger> <reply>` — Auto-reply\n"
            "`-listreplies` — View all auto-replies\n"
            "`-delreply <id>` — Delete an auto-reply"
        ), inline=False)

    e.set_footer(text="Every message gives you a chance to win a prize AND earns credits!")
    await ctx.send(embed=e)

# ─── Run ──────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
