import os
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

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

# ─── DB Init ──────────────────────────────────────────────────────────────────
def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""

                -- ─── Characters ───────────────────────────────────────────
                -- One row per Discord user. Stores base stats before
                -- equipment bonuses are applied.
                CREATE TABLE IF NOT EXISTS characters (
                    user_id     BIGINT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    level       INT DEFAULT 1,
                    exp         BIGINT DEFAULT 0,
                    exp_needed  BIGINT DEFAULT 100,
                    hp          INT DEFAULT 100,
                    max_hp      INT DEFAULT 100,
                    atk         INT DEFAULT 10,
                    def         INT DEFAULT 5,
                    gold        BIGINT DEFAULT 0,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );

                -- ─── Items ─────────────────────────────────────────────────
                -- Master catalog of every item in the game.
                -- Stats here are the BONUS the item provides when equipped.
                -- min_level = minimum character level required to equip/trade.
                -- source: 'drop', 'shop', 'craft', or 'all'
                CREATE TABLE IF NOT EXISTS items (
                    id          SERIAL PRIMARY KEY,
                    name        TEXT UNIQUE NOT NULL,
                    type        TEXT NOT NULL,      -- weapon / armor / accessory / material / consumable
                    rarity      TEXT NOT NULL,      -- common / uncommon / rare / epic / legendary
                    min_level   INT DEFAULT 1,
                    atk_bonus   INT DEFAULT 0,
                    def_bonus   INT DEFAULT 0,
                    hp_bonus    INT DEFAULT 0,
                    gold_value  INT DEFAULT 0,      -- base shop sell/buy price
                    source      TEXT DEFAULT 'all', -- where it can come from
                    description TEXT,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );

                -- ─── Equipment ─────────────────────────────────────────────
                -- What each player currently has equipped.
                -- One row per slot per user. item_id is NULL if slot is empty.
                CREATE TABLE IF NOT EXISTS equipment (
                    user_id     BIGINT NOT NULL,
                    slot        TEXT NOT NULL,      -- weapon / armor / accessory
                    item_id     INT REFERENCES items(id) ON DELETE SET NULL,
                    PRIMARY KEY (user_id, slot)
                );

                -- ─── Inventory ─────────────────────────────────────────────
                -- All items a player owns (not equipped).
                -- Stackable items (materials, consumables) use quantity > 1.
                CREATE TABLE IF NOT EXISTS inventory (
                    id          SERIAL PRIMARY KEY,
                    user_id     BIGINT NOT NULL,
                    item_id     INT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    quantity    INT DEFAULT 1,
                    obtained_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (user_id, item_id)
                );

                -- ─── Crafting Recipes ───────────────────────────────────────
                -- Each recipe produces one output item.
                -- ingredients stored as JSONB: [{"item_id": 1, "qty": 2}, ...]
                CREATE TABLE IF NOT EXISTS recipes (
                    id              SERIAL PRIMARY KEY,
                    result_item_id  INT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    ingredients     JSONB NOT NULL,
                    gold_cost       INT DEFAULT 0,
                    min_level       INT DEFAULT 1,
                    description     TEXT
                );

                -- ─── Dungeons ──────────────────────────────────────────────
                -- Each dungeon has N waves of enemies + 1 boss wave.
                -- min_level = minimum level to enter.
                -- wave_count = number of normal waves before the boss.
                CREATE TABLE IF NOT EXISTS dungeons (
                    id          SERIAL PRIMARY KEY,
                    name        TEXT UNIQUE NOT NULL,
                    description TEXT,
                    min_level   INT DEFAULT 1,
                    wave_count  INT DEFAULT 5,
                    gold_reward INT DEFAULT 50,     -- base gold on completion
                    exp_reward  INT DEFAULT 100,    -- base EXP on completion
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );

                -- ─── Enemies ───────────────────────────────────────────────
                -- Enemies belong to a dungeon and a specific wave number.
                -- wave = 0 means this enemy is the boss.
                -- count = how many of this enemy spawn in that wave.
                CREATE TABLE IF NOT EXISTS enemies (
                    id          SERIAL PRIMARY KEY,
                    dungeon_id  INT NOT NULL REFERENCES dungeons(id) ON DELETE CASCADE,
                    name        TEXT NOT NULL,
                    wave        INT NOT NULL,       -- 0 = boss
                    count       INT DEFAULT 1,
                    hp          INT NOT NULL,
                    atk         INT NOT NULL,
                    def         INT NOT NULL,
                    exp_reward  INT DEFAULT 10,
                    gold_reward INT DEFAULT 5
                );

                -- ─── Loot Table ────────────────────────────────────────────
                -- Items that can drop from a dungeon on completion.
                -- drop_chance is 0.0 to 1.0 (e.g. 0.05 = 5% chance).
                -- Each player rolls separately against this table.
                CREATE TABLE IF NOT EXISTS loot_table (
                    id          SERIAL PRIMARY KEY,
                    dungeon_id  INT NOT NULL REFERENCES dungeons(id) ON DELETE CASCADE,
                    item_id     INT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    drop_chance FLOAT NOT NULL,     -- 0.0 to 1.0
                    UNIQUE (dungeon_id, item_id)
                );

                -- ─── Parties ───────────────────────────────────────────────
                -- A party is created by a leader and holds up to 4 members.
                -- status: 'waiting' | 'in_progress' | 'completed' | 'disbanded'
                -- is_open: TRUE = anyone can join with the code
                -- code: 6-char alphanumeric join code
                CREATE TABLE IF NOT EXISTS parties (
                    id          SERIAL PRIMARY KEY,
                    leader_id   BIGINT NOT NULL,
                    dungeon_id  INT REFERENCES dungeons(id) ON DELETE SET NULL,
                    status      TEXT DEFAULT 'waiting',
                    is_open     BOOLEAN DEFAULT FALSE,
                    code        TEXT UNIQUE,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );

                -- ─── Party Members ─────────────────────────────────────────
                -- Maps users to parties. Max 4 rows per party_id.
                CREATE TABLE IF NOT EXISTS party_members (
                    party_id    INT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
                    user_id     BIGINT NOT NULL,
                    joined_at   TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (party_id, user_id)
                );

                -- ─── Dungeon Runs ──────────────────────────────────────────
                -- One row per dungeon attempt by a party.
                -- current_wave tracks progress (0 = not started, 99 = boss done).
                -- status: 'in_progress' | 'completed' | 'failed'
                CREATE TABLE IF NOT EXISTS dungeon_runs (
                    id              SERIAL PRIMARY KEY,
                    party_id        INT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
                    dungeon_id      INT NOT NULL REFERENCES dungeons(id) ON DELETE CASCADE,
                    status          TEXT DEFAULT 'in_progress',
                    current_wave    INT DEFAULT 1,
                    started_at      TIMESTAMPTZ DEFAULT NOW(),
                    finished_at     TIMESTAMPTZ
                );

                -- ─── Run Results ───────────────────────────────────────────
                -- Per-player results after a dungeon run completes.
                -- items_earned stored as JSONB: [item_id, item_id, ...]
                CREATE TABLE IF NOT EXISTS run_results (
                    id              SERIAL PRIMARY KEY,
                    run_id          INT NOT NULL REFERENCES dungeon_runs(id) ON DELETE CASCADE,
                    user_id         BIGINT NOT NULL,
                    damage_dealt    BIGINT DEFAULT 0,
                    exp_earned      INT DEFAULT 0,
                    gold_earned     INT DEFAULT 0,
                    items_earned    JSONB DEFAULT '[]'
                );

                -- ─── Trade Listings ────────────────────────────────────────
                -- A player lists an item for sale to another player.
                -- status: 'open' | 'completed' | 'cancelled'
                -- tax is calculated at completion based on rarity.
                -- Tax rates: common=2% uncommon=5% rare=10% epic=15% legendary=25%
                CREATE TABLE IF NOT EXISTS trades (
                    id              SERIAL PRIMARY KEY,
                    seller_id       BIGINT NOT NULL,
                    buyer_id        BIGINT,             -- NULL = open to anyone
                    item_id         INT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    quantity        INT DEFAULT 1,
                    price           BIGINT NOT NULL,    -- gold asking price
                    tax_rate        FLOAT NOT NULL,     -- e.g. 0.10 for 10%
                    status          TEXT DEFAULT 'open',
                    listed_at       TIMESTAMPTZ DEFAULT NOW(),
                    completed_at    TIMESTAMPTZ
                );

                -- ─── Shop Listings ─────────────────────────────────────────
                -- Admin-managed shop. stock = -1 means infinite.
                CREATE TABLE IF NOT EXISTS shop (
                    id          SERIAL PRIMARY KEY,
                    item_id     INT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    price       INT NOT NULL,
                    stock       INT DEFAULT -1,
                    UNIQUE (item_id)
                );

            """)

            # ── Safe migrations (add columns if they don't exist) ──────────
            migrations = [
                "ALTER TABLE characters ADD COLUMN IF NOT EXISTS exp_needed BIGINT DEFAULT 100;",
            ]
            for sql in migrations:
                cur.execute(sql)

        conn.commit()
    print("✅ Database tables ready")


# ─── Tax Rates by Rarity ──────────────────────────────────────────────────────
TRADE_TAX = {
    "common":    0.02,
    "uncommon":  0.05,
    "rare":      0.10,
    "epic":      0.15,
    "legendary": 0.25,
}

# ─── EXP needed to reach next level ──────────────────────────────────────────
def exp_for_level(level: int) -> int:
    """Returns EXP needed to level up FROM this level."""
    return int(100 * (level ** 1.5))

# ─── Computed stats (base + equipment bonuses) ────────────────────────────────
def get_total_stats(user_id: int) -> dict:
    """Returns a character's full stats including all equipped item bonuses."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM characters WHERE user_id=%s", (user_id,))
            char = cur.fetchone()
            if not char:
                return {}
            cur.execute("""
                SELECT i.atk_bonus, i.def_bonus, i.hp_bonus
                FROM equipment e
                JOIN items i ON e.item_id = i.id
                WHERE e.user_id = %s AND e.item_id IS NOT NULL
            """, (user_id,))
            bonuses = cur.fetchall()
    total_atk = char["atk"] + sum(b["atk_bonus"] for b in bonuses)
    total_def = char["def"] + sum(b["def_bonus"] for b in bonuses)
    total_hp  = char["max_hp"] + sum(b["hp_bonus"] for b in bonuses)
    return {
        "user_id":  user_id,
        "name":     char["name"],
        "level":    char["level"],
        "exp":      char["exp"],
        "exp_needed": exp_for_level(char["level"]),
        "hp":       char["hp"],
        "max_hp":   total_hp,
        "atk":      total_atk,
        "def":      total_def,
        "gold":     char["gold"],
    }
