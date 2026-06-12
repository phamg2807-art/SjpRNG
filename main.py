import os
import sys
import json
import time
import math
import random
import asyncio
import hashlib
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
        _pool = ThreadedConnectionPool(2, 10, dsn=DB_URL, cursor_factory=RealDictCursor,
                                       sslmode='require', connect_timeout=10)
    return _pool

def db():
    conn = get_pool().getconn()
    return conn

def release(conn):
    get_pool().putconn(conn)

# ─── Game Constants ────────────────────────────────────────────────────────────
CRATE_COST        = 100    # credits
CRATE_FEE_PCT     = 0.05   # 5% to bank on crate purchase
TRADE_TAX_PCT     = 0.10   # 10% tax on trades
MARKET_FEE_PCT    = 0.10   # 10% auction fee on sale
DAILY_BANK_SHARE  = True   # distribute bank daily
CREDITS_PER_MSG   = 1
MSG_COOLDOWN_S    = 30     # seconds between credit-earning messages

# ─── Coin Attribute Tables ────────────────────────────────────────────────────
MATERIALS = [
    ("Plastic",   0.50,  10),
    ("Wood",      0.75,   8),
    ("Stone",     1.00,   2),   # 1/2 → weight 2 out of 20 → normalized below
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

FLOATS = [
    ("Bad",      0.50,  15),
    ("Good",     1.00,   2),
    ("Great",    2.00,   4),
    ("Amazing",  3.00,   8),
    ("Heavenly",15.00,  50),
    ("Godlike", 30.00, 100),
]

def weighted_choice(table):
    """table: list of (name, multiplier, denominator)"""
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
    # True-rounded (1000, 2000, …)
    if s > 0 and s % 1000 == 0:
        return s, 10.0
    # Contains 9999
    if s == 9999:
        return s, 10.0
    # Contains 999
    if s % 1000 == 999 or (s >= 999 and str(999) in str(s)):
        return s, 10.0
    # Contains 99
    if s % 100 == 99 or (s >= 99 and str(99) in str(s)):
        return s, 10.0
    # Under 10
    if s < 10:
        return s, 5.0
    # Under 100
    if s < 100:
        return s, 3.0
    # Round serial (divisible by common round numbers)
    if s % 500 == 0 or s % 250 == 0 or s % 100 == 0:
        return s, 2.0
    return s, 1.0

def generate_coin():
    material,  mat_mult  = weighted_choice(MATERIALS)
    variant,   var_mult  = weighted_choice(VARIANTS)
    status,    sta_mult  = weighted_choice(STATUSES)
    float_name,flt_mult  = weighted_choice(FLOATS)
    serial_num = random.randint(0, 9999)
    _, ser_mult = serial_multiplier(serial_num)

    base_value = round(random.uniform(1.0, 5.0), 2)
    total_mult = mat_mult * var_mult * sta_mult * flt_mult * ser_mult
    final_value = round(base_value * total_mult, 4)

    return {
        "material":   material,
        "variant":    variant,
        "status":     status,
        "float":      float_name,
        "serial":     serial_num,
        "base_value": base_value,
        "mat_mult":   mat_mult,
        "var_mult":   var_mult,
        "sta_mult":   sta_mult,
        "flt_mult":   flt_mult,
        "ser_mult":   ser_mult,
        "total_mult": round(total_mult, 4),
        "value":      final_value,
    }

def coin_name(coin_row):
    return f"{coin_row['variant']} {coin_row['material']} Coin"

def coin_display(coin_row, show_id=True):
    serial_str = str(coin_row['serial']).zfill(4)
    prefix = f"`#{coin_row['id']}` " if show_id and 'id' in coin_row else ""
    return (
        f"{prefix}**{coin_row['variant']} {coin_row['material']} Coin** #{serial_str}\n"
        f"  Status: **{coin_row['status']}** | Float: **{coin_row['float']}**\n"
        f"  Base: **${coin_row['base_value']:.2f}** × {coin_row['total_mult']:.4f} = **${coin_row['value']:.4f}**"
    )

# ─── DB Init ──────────────────────────────────────────────────────────────────
def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     BIGINT PRIMARY KEY,
            username    TEXT,
            credits     INT     DEFAULT 0,
            cash        FLOAT   DEFAULT 0,
            total_coins INT     DEFAULT 0,
            last_msg_ts BIGINT  DEFAULT 0,
            last_daily  DATE,
            joined_at   TIMESTAMP DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS coins (
            id         SERIAL PRIMARY KEY,
            owner_id   BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            material   TEXT,
            variant    TEXT,
            status     TEXT,
            float      TEXT,
            serial     INT,
            base_value FLOAT,
            mat_mult   FLOAT,
            var_mult   FLOAT,
            sta_mult   FLOAT,
            flt_mult   FLOAT,
            ser_mult   FLOAT,
            total_mult FLOAT,
            value      FLOAT,
            obtained_at TIMESTAMP DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id           SERIAL PRIMARY KEY,
            initiator_id BIGINT,
            receiver_id  BIGINT,
            coin_ids     TEXT,
            cash_offer   FLOAT   DEFAULT 0,
            status       TEXT    DEFAULT 'pending',
            created_at   TIMESTAMP DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS auctions (
            id           SERIAL PRIMARY KEY,
            seller_id    BIGINT,
            coin_id      INT,
            start_price  FLOAT,
            current_bid  FLOAT   DEFAULT 0,
            bidder_id    BIGINT,
            ends_at      TIMESTAMP,
            status       TEXT    DEFAULT 'active',
            created_at   TIMESTAMP DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bank (
            id    INT PRIMARY KEY DEFAULT 1,
            total FLOAT DEFAULT 0
        );
        INSERT INTO bank (id, total) VALUES (1, 0) ON CONFLICT DO NOTHING;
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bank_log (
            id        SERIAL PRIMARY KEY,
            source    TEXT,
            amount    FLOAT,
            logged_at TIMESTAMP DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_log (
            paid_date DATE PRIMARY KEY,
            amount    FLOAT,
            paid_at   TIMESTAMP DEFAULT NOW()
        );
    """)

    conn.commit()
    release(conn)
    print("✅ Database initialized.")

# ─── Helpers ──────────────────────────────────────────────────────────────────
def ensure_user(user_id: int, username: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, username) VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
    """, (user_id, username))
    conn.commit()
    release(conn)

def get_user(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    release(conn)
    return row

def add_credits(user_id: int, amount: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET credits = credits + %s WHERE user_id = %s", (amount, user_id))
    conn.commit()
    release(conn)

def add_cash(user_id: int, amount: float):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET cash = cash + %s WHERE user_id = %s", (amount, user_id))
    conn.commit()
    release(conn)

def add_to_bank(amount: float, source: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE bank SET total = total + %s WHERE id = 1", (amount,))
    cur.execute("INSERT INTO bank_log (source, amount) VALUES (%s, %s)", (source, amount))
    conn.commit()
    release(conn)

def get_bank():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT total FROM bank WHERE id = 1")
    row = cur.fetchone()
    release(conn)
    return row['total'] if row else 0.0

def count_users():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM users")
    row = cur.fetchone()
    release(conn)
    return row['c'] if row else 1

def tier_emoji(value: float):
    if value >= 500:   return "🌌"
    if value >= 100:   return "👑"
    if value >= 50:    return "💎"
    if value >= 20:    return "🔥"
    if value >= 10:    return "⭐"
    if value >= 5:     return "🟡"
    if value >= 2:     return "🔵"
    if value >= 1:     return "⚪"
    return "🟤"

# ─── Spam Prevention ──────────────────────────────────────────────────────────
MSG_COOLDOWNS = {}  # user_id → last credit-earning timestamp (monotonic)

# ─── Bot Setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='-', intents=intents, help_command=None)

# ─── Message Listener (Credits) ───────────────────────────────────────────────
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    uid = message.author.id
    now = time.monotonic()

    # Register user if not seen before
    ensure_user(uid, str(message.author))

    last = MSG_COOLDOWNS.get(uid, 0)
    if now - last >= MSG_COOLDOWN_S:
        MSG_COOLDOWNS[uid] = now
        add_credits(uid, CREDITS_PER_MSG)

    await bot.process_commands(message)

# ─── Background Tasks ─────────────────────────────────────────────────────────
@tasks.loop(minutes=30)
async def auction_checker():
    """Finalize expired auctions."""
    conn = db()
    cur = conn.cursor()
    now = datetime.now(timezone.utc)
    cur.execute("SELECT * FROM auctions WHERE status = 'active' AND ends_at <= %s", (now,))
    expired = cur.fetchall()

    for a in expired:
        coin_id = a['coin_id']
        seller_id = a['seller_id']

        if a['bidder_id']:
            winner_id = a['bidder_id']
            sale_price = a['current_bid']
            fee = round(sale_price * MARKET_FEE_PCT, 4)
            seller_receives = round(sale_price - fee, 4)

            # Transfer coin
            cur.execute("UPDATE coins SET owner_id = %s WHERE id = %s", (winner_id, coin_id))
            # Pay seller
            cur.execute("UPDATE users SET cash = cash + %s WHERE user_id = %s", (seller_receives, seller_id))
            # Bank gets fee
            cur.execute("UPDATE bank SET total = total + %s WHERE id = 1", (fee,))
            cur.execute("INSERT INTO bank_log (source, amount) VALUES (%s, %s)",
                        (f"auction_fee:{a['id']}", fee))
            # Update winner coin count
            cur.execute("UPDATE users SET total_coins = (SELECT COUNT(*) FROM coins WHERE owner_id = %s) WHERE user_id = %s",
                        (winner_id, winner_id))
            cur.execute("UPDATE users SET total_coins = (SELECT COUNT(*) FROM coins WHERE owner_id = %s) WHERE user_id = %s",
                        (seller_id, seller_id))
            cur.execute("UPDATE auctions SET status = 'sold' WHERE id = %s", (a['id'],))

            # Notify winner & seller via DM
            try:
                winner = await bot.fetch_user(winner_id)
                conn2 = db()
                cur2 = conn2.cursor()
                cur2.execute("SELECT * FROM coins WHERE id = %s", (coin_id,))
                c = cur2.fetchone()
                release(conn2)
                if winner and c:
                    await winner.send(f"🎉 You won auction **#{a['id']}**! **{coin_name(c)}** is now yours. Paid: **${sale_price:.4f}**")
            except:
                pass
            try:
                seller = await bot.fetch_user(seller_id)
                if seller:
                    await seller.send(f"✅ Your auction **#{a['id']}** sold for **${sale_price:.4f}**. You received **${seller_receives:.4f}** after fees.")
            except:
                pass
        else:
            # No bids — return coin to seller
            cur.execute("UPDATE coins SET owner_id = %s WHERE id = %s", (seller_id, coin_id))
            cur.execute("UPDATE auctions SET status = 'expired' WHERE id = %s", (a['id'],))
            try:
                seller = await bot.fetch_user(seller_id)
                if seller:
                    await seller.send(f"📦 Your auction **#{a['id']}** expired with no bids. The coin has been returned.")
            except:
                pass

    conn.commit()
    release(conn)

@tasks.loop(hours=24)
async def daily_bank_distribution():
    """Distribute bank balance equally to all users once a day."""
    bank_total = get_bank()
    if bank_total <= 0:
        return

    n = count_users()
    if n == 0:
        return

    share = round(bank_total / n, 4)
    conn = db()
    cur = conn.cursor()
    today = datetime.now(timezone.utc).date()

    cur.execute("SELECT paid_date FROM daily_log WHERE paid_date = %s", (today,))
    if cur.fetchone():
        release(conn)
        return

    cur.execute("UPDATE users SET cash = cash + %s", (share,))
    cur.execute("UPDATE bank SET total = 0 WHERE id = 1")
    cur.execute("INSERT INTO daily_log (paid_date, amount) VALUES (%s, %s)", (today, share))
    conn.commit()
    release(conn)
    print(f"✅ Daily bank payout: ${share:.4f} to {n} users. Bank was ${bank_total:.4f}")

# ─── Trade View ───────────────────────────────────────────────────────────────
class TradeView(discord.ui.View):
    def __init__(self, trade_id: int, initiator_id: int, receiver_id: int):
        super().__init__(timeout=120)
        self.trade_id = trade_id
        self.initiator_id = initiator_id
        self.receiver_id = receiver_id

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
        cur.execute("SELECT * FROM trades WHERE id = %s AND status = 'pending'", (self.trade_id,))
        trade = cur.fetchone()

        if not trade:
            await interaction.response.send_message("⚠️ Trade no longer active.", ephemeral=True)
            release(conn)
            return

        if not accepted:
            cur.execute("UPDATE trades SET status = 'declined' WHERE id = %s", (self.trade_id,))
            conn.commit()
            release(conn)
            await interaction.response.edit_message(content="❌ Trade declined.", embed=None, view=None)
            return

        coin_ids = [int(x) for x in trade['coin_ids'].split(',') if x.strip()]
        cash_offer = trade['cash_offer']

        # Validate all coins still belong to initiator
        if coin_ids:
            cur.execute("SELECT id, owner_id FROM coins WHERE id = ANY(%s)", (coin_ids,))
            rows = cur.fetchall()
            for r in rows:
                if r['owner_id'] != trade['initiator_id']:
                    cur.execute("UPDATE trades SET status = 'invalid' WHERE id = %s", (self.trade_id,))
                    conn.commit()
                    release(conn)
                    await interaction.response.edit_message(content="❌ Coin ownership changed; trade cancelled.", embed=None, view=None)
                    return

        # Validate cash
        if cash_offer > 0:
            cur.execute("SELECT cash FROM users WHERE user_id = %s", (trade['initiator_id'],))
            init_user = cur.fetchone()
            if not init_user or init_user['cash'] < cash_offer:
                cur.execute("UPDATE trades SET status = 'invalid' WHERE id = %s", (self.trade_id,))
                conn.commit()
                release(conn)
                await interaction.response.edit_message(content="❌ Initiator has insufficient cash.", embed=None, view=None)
                return

        # Execute
        if coin_ids:
            cur.execute("UPDATE coins SET owner_id = %s WHERE id = ANY(%s)", (trade['receiver_id'], coin_ids))

        if cash_offer > 0:
            tax = round(cash_offer * TRADE_TAX_PCT, 4)
            net = round(cash_offer - tax, 4)
            cur.execute("UPDATE users SET cash = cash - %s WHERE user_id = %s", (cash_offer, trade['initiator_id']))
            cur.execute("UPDATE users SET cash = cash + %s WHERE user_id = %s", (net, trade['receiver_id']))
            cur.execute("UPDATE bank SET total = total + %s WHERE id = 1", (tax,))
            cur.execute("INSERT INTO bank_log (source, amount) VALUES (%s, %s)", (f"trade_tax:{self.trade_id}", tax))

        # Refresh coin counts
        for uid in (trade['initiator_id'], trade['receiver_id']):
            cur.execute("UPDATE users SET total_coins = (SELECT COUNT(*) FROM coins WHERE owner_id = %s) WHERE user_id = %s", (uid, uid))

        cur.execute("UPDATE trades SET status = 'completed' WHERE id = %s", (self.trade_id,))
        conn.commit()
        release(conn)

        embed = discord.Embed(title="✅ Trade Completed!", color=discord.Color.green())
        embed.description = f"Trade **#{self.trade_id}** was accepted and processed."
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

# ─── Auction Bid View ─────────────────────────────────────────────────────────
class BidModal(discord.ui.Modal, title="Place a Bid"):
    amount = discord.ui.TextInput(label="Bid Amount ($)", placeholder="e.g. 12.50")

    def __init__(self, auction_id: int):
        super().__init__()
        self.auction_id = auction_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            bid = float(self.amount.value)
        except ValueError:
            await interaction.response.send_message("❌ Invalid amount.", ephemeral=True)
            return

        uid = interaction.user.id
        ensure_user(uid, str(interaction.user))

        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM auctions WHERE id = %s AND status = 'active'", (self.auction_id,))
        a = cur.fetchone()

        if not a:
            await interaction.response.send_message("❌ Auction not found or ended.", ephemeral=True)
            release(conn)
            return

        if uid == a['seller_id']:
            await interaction.response.send_message("❌ You can't bid on your own auction.", ephemeral=True)
            release(conn)
            return

        min_bid = max(a['start_price'], (a['current_bid'] or 0) + 0.01)
        if bid < min_bid:
            await interaction.response.send_message(f"❌ Minimum bid is **${min_bid:.4f}**.", ephemeral=True)
            release(conn)
            return

        cur.execute("SELECT cash FROM users WHERE user_id = %s", (uid,))
        user = cur.fetchone()
        if not user or user['cash'] < bid:
            await interaction.response.send_message(f"❌ Insufficient cash. You have **${user['cash']:.4f}**.", ephemeral=True)
            release(conn)
            return

        # Refund previous bidder
        if a['bidder_id'] and a['current_bid']:
            cur.execute("UPDATE users SET cash = cash + %s WHERE user_id = %s", (a['current_bid'], a['bidder_id']))

        # Reserve bid amount
        cur.execute("UPDATE users SET cash = cash - %s WHERE user_id = %s", (bid, uid))
        cur.execute("UPDATE auctions SET current_bid = %s, bidder_id = %s WHERE id = %s",
                    (bid, uid, self.auction_id))
        conn.commit()
        release(conn)

        await interaction.response.send_message(
            f"✅ Bid of **${bid:.4f}** placed on auction **#{self.auction_id}**!", ephemeral=True
        )

class AuctionView(discord.ui.View):
    def __init__(self, auction_id: int):
        super().__init__(timeout=None)
        self.auction_id = auction_id

    @discord.ui.button(label="💰 Place Bid", style=discord.ButtonStyle.blurple)
    async def bid(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BidModal(self.auction_id))

# ─── COMMANDS ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    init_db()
    auction_checker.start()
    daily_bank_distribution.start()
    print(f"✅ CoinVault logged in as {bot.user}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument. Use `-help` for usage.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Bad argument type.")
    else:
        raise error

# ── -help ─────────────────────────────────────────────────────────────────────
@bot.command()
async def help(ctx):
    e = discord.Embed(title="🪙 CoinVault — Command Reference", color=0x5865F2)
    e.add_field(name="📦 Economy", value=(
        "`-balance` — Your credits & cash\n"
        "`-buy crate` — Spend 100 credits to open a coin crate (5% bank fee)\n"
        "`-daily` — Claim your bank share distribution\n"
    ), inline=False)
    e.add_field(name="🎒 Inventory", value=(
        "`-inventory [page]` / `-inv` — View your coins\n"
        "`-coin <id>` — Detailed view of a coin\n"
        "`-sell <coin_id>` — Sell coin for its cash value\n"
    ), inline=False)
    e.add_field(name="🤝 Trading", value=(
        "`-trade @user [coin_ids] [cash:<amount>]` — Offer trade\n"
        "  e.g. `-trade @Bob 12,15 cash:5.00`\n"
        "`-trades` — View your active pending trades\n"
    ), inline=False)
    e.add_field(name="🏪 Marketplace", value=(
        "`-market [page]` — Browse active auctions\n"
        "`-auction <coin_id> <start_price> [hours]` — List a coin (10% fee on sale)\n"
        "`-myauctions` — View your active listings\n"
        "`-cancelauction <id>` — Cancel your auction\n"
    ), inline=False)
    e.add_field(name="📊 Stats & Social", value=(
        "`-profile [@user]` — View profile\n"
        "`-leaderboard` / `-lb` — Top coin holders by value\n"
        "`-bank` — View bank treasury balance\n"
        "`-stats` — Your earning stats\n"
    ), inline=False)
    e.set_footer(text="Earn 1 credit per message (30s cooldown) • 100 credits = 1 crate")
    await ctx.send(embed=e)

# ── -balance ──────────────────────────────────────────────────────────────────
@bot.command(aliases=['bal', 'wallet'])
async def balance(ctx):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    u = get_user(uid)
    e = discord.Embed(title=f"💳 {ctx.author.display_name}'s Wallet", color=0x57F287)
    e.add_field(name="🎟️ Credits", value=f"**{u['credits']:,}**", inline=True)
    e.add_field(name="💵 Cash", value=f"**${u['cash']:.4f}**", inline=True)
    e.add_field(name="🪙 Coins Owned", value=f"**{u['total_coins']}**", inline=True)
    await ctx.send(embed=e)

# ── -buy crate ────────────────────────────────────────────────────────────────
@bot.command()
async def buy(ctx, item: str = None):
    if item is None or item.lower() != 'crate':
        await ctx.send("❌ Usage: `-buy crate`")
        return

    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    u = get_user(uid)

    if u['credits'] < CRATE_COST:
        await ctx.send(f"❌ You need **{CRATE_COST} credits** to buy a crate. You have **{u['credits']}**.")
        return

    # Deduct credits + bank fee (5%)
    fee = round(CRATE_COST * CRATE_FEE_PCT)   # credits fee converted conceptually; bank gets cash value
    cash_fee = round(CRATE_COST * CRATE_FEE_PCT * 0.1, 4)  # symbolic bank cash contribution

    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET credits = credits - %s WHERE user_id = %s", (CRATE_COST, uid))
    conn.commit()
    release(conn)

    add_to_bank(cash_fee, f"crate_fee:user_{uid}")

    coin = generate_coin()

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO coins (owner_id, material, variant, status, float, serial,
                           base_value, mat_mult, var_mult, sta_mult, flt_mult, ser_mult, total_mult, value)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
    """, (uid, coin['material'], coin['variant'], coin['status'], coin['float'], coin['serial'],
          coin['base_value'], coin['mat_mult'], coin['var_mult'], coin['sta_mult'],
          coin['flt_mult'], coin['ser_mult'], coin['total_mult'], coin['value']))
    row = cur.fetchone()
    cur.execute("UPDATE users SET total_coins = (SELECT COUNT(*) FROM coins WHERE owner_id = %s) WHERE user_id = %s", (uid, uid))
    conn.commit()
    release(conn)

    coin['id'] = row['id']
    serial_str = str(coin['serial']).zfill(4)
    tier = tier_emoji(coin['value'])

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
        f"**Final: ${coin['value']:.4f}**"
    ), inline=True)
    e.set_footer(text=f"Coin ID: #{row['id']} • Credits left: {u['credits'] - CRATE_COST}")
    await ctx.send(embed=e)

# ── -inventory ────────────────────────────────────────────────────────────────
@bot.command(aliases=['inv'])
async def inventory(ctx, page: int = 1):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))

    per_page = 8
    offset = (page - 1) * per_page

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM coins WHERE owner_id = %s", (uid,))
    total = cur.fetchone()['c']
    cur.execute("SELECT * FROM coins WHERE owner_id = %s ORDER BY value DESC LIMIT %s OFFSET %s",
                (uid, per_page, offset))
    coins = cur.fetchall()
    release(conn)

    if not coins:
        await ctx.send("🎒 Your inventory is empty! Use `-buy crate` to open one.")
        return

    pages = math.ceil(total / per_page)
    e = discord.Embed(title=f"🎒 {ctx.author.display_name}'s Inventory", color=0x5865F2)
    e.description = f"Showing page **{page}/{pages}** | Total coins: **{total}**\n\n"
    e.description += "\n\n".join(coin_display(c) for c in coins)
    e.set_footer(text=f"Use -coin <id> for details • -inventory {page+1} for next page")
    await ctx.send(embed=e)

# ── -coin <id> ────────────────────────────────────────────────────────────────
@bot.command()
async def coin(ctx, coin_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT c.*, u.username FROM coins c JOIN users u ON u.user_id = c.owner_id WHERE c.id = %s", (coin_id,))
    c = cur.fetchone()
    release(conn)

    if not c:
        await ctx.send(f"❌ Coin #{coin_id} not found.")
        return

    serial_str = str(c['serial']).zfill(4)
    tier = tier_emoji(c['value'])
    e = discord.Embed(title=f"{tier} Coin #{coin_id} — {c['variant']} {c['material']}", color=0xFFD700)
    e.add_field(name="Owner", value=c['username'], inline=True)
    e.add_field(name="Serial", value=f"#{serial_str}", inline=True)
    e.add_field(name="Obtained", value=c['obtained_at'].strftime("%Y-%m-%d"), inline=True)
    e.add_field(name="📊 Attributes & Multipliers", value=(
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

# ── -sell <coin_id> ───────────────────────────────────────────────────────────
@bot.command()
async def sell(ctx, coin_id: int):
    uid = ctx.author.id

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM coins WHERE id = %s AND owner_id = %s", (coin_id, uid))
    c = cur.fetchone()

    if not c:
        await ctx.send(f"❌ Coin #{coin_id} not found in your inventory.")
        release(conn)
        return

    # Check if in active auction
    cur.execute("SELECT id FROM auctions WHERE coin_id = %s AND status = 'active'", (coin_id,))
    if cur.fetchone():
        await ctx.send(f"❌ Coin #{coin_id} is currently listed in an auction. Cancel it first.")
        release(conn)
        return

    val = c['value']
    cur.execute("DELETE FROM coins WHERE id = %s", (coin_id,))
    cur.execute("UPDATE users SET cash = cash + %s, total_coins = total_coins - 1 WHERE user_id = %s", (val, uid))
    conn.commit()
    release(conn)

    e = discord.Embed(title="💸 Coin Sold!", color=0x57F287)
    e.description = f"Sold **{c['variant']} {c['material']} Coin** `#{str(c['serial']).zfill(4)}`\nReceived: **${val:.4f}**"
    await ctx.send(embed=e)

# ── -trade @user [coin_ids] [cash:<amount>] ────────────────────────────────────
@bot.command()
async def trade(ctx, member: discord.Member, *, args: str = ""):
    if member.bot or member.id == ctx.author.id:
        await ctx.send("❌ Invalid trade target.")
        return

    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))
    ensure_user(member.id, str(member))

    # Parse args: "1,2,3 cash:5.00" or just "1,2,3" or just "cash:5.00"
    coin_ids = []
    cash_offer = 0.0
    parts = args.strip().split()

    for part in parts:
        if part.lower().startswith("cash:"):
            try:
                cash_offer = float(part.split(":")[1])
            except:
                await ctx.send("❌ Invalid cash format. Use `cash:5.00`")
                return
        elif part:
            try:
                coin_ids = [int(x.strip()) for x in part.split(",") if x.strip()]
            except:
                await ctx.send("❌ Invalid coin IDs. Use comma-separated numbers.")
                return

    if not coin_ids and cash_offer == 0:
        await ctx.send("❌ Specify at least coin IDs or a cash offer. E.g.: `-trade @Bob 12,15 cash:5`")
        return

    # Validate coins belong to initiator
    if coin_ids:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM coins WHERE id = ANY(%s) AND owner_id = %s", (coin_ids, uid))
        found = [r['id'] for r in cur.fetchall()]
        release(conn)
        invalid = set(coin_ids) - set(found)
        if invalid:
            await ctx.send(f"❌ Coin(s) `{invalid}` not in your inventory.")
            return

    # Validate cash
    if cash_offer > 0:
        u = get_user(uid)
        if u['cash'] < cash_offer:
            await ctx.send(f"❌ Insufficient cash. You have **${u['cash']:.4f}**.")
            return

    # Create trade record
    conn = db()
    cur = conn.cursor()
    coin_ids_str = ",".join(str(x) for x in coin_ids)
    cur.execute("""
        INSERT INTO trades (initiator_id, receiver_id, coin_ids, cash_offer)
        VALUES (%s, %s, %s, %s) RETURNING id
    """, (uid, member.id, coin_ids_str, cash_offer))
    trade_id = cur.fetchone()['id']
    conn.commit()
    release(conn)

    # Build embed
    e = discord.Embed(title=f"🤝 Trade Offer #{trade_id}", color=0xFEE75C)
    e.description = f"**{ctx.author.display_name}** wants to trade with **{member.display_name}**"
    offer_lines = []
    if coin_ids:
        offer_lines.append(f"Coins: `{', '.join('#'+str(i) for i in coin_ids)}`")
    if cash_offer > 0:
        tax = round(cash_offer * TRADE_TAX_PCT, 4)
        net = round(cash_offer - tax, 4)
        offer_lines.append(f"Cash: **${cash_offer:.4f}** (receiver gets **${net:.4f}** after 10% tax)")
    e.add_field(name="📤 Offer from sender", value="\n".join(offer_lines) or "None", inline=False)
    e.set_footer(text="Trade expires in 2 minutes")

    view = TradeView(trade_id, uid, member.id)
    await ctx.send(f"{member.mention}", embed=e, view=view)

# ── -trades ───────────────────────────────────────────────────────────────────
@bot.command()
async def trades(ctx):
    uid = ctx.author.id
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM trades WHERE (initiator_id = %s OR receiver_id = %s) AND status = 'pending'
        ORDER BY created_at DESC LIMIT 10
    """, (uid, uid))
    rows = cur.fetchall()
    release(conn)

    if not rows:
        await ctx.send("📭 No pending trades.")
        return

    e = discord.Embed(title="📋 Your Pending Trades", color=0xFEE75C)
    for t in rows:
        role = "Sender" if t['initiator_id'] == uid else "Receiver"
        e.add_field(
            name=f"Trade #{t['id']} [{role}]",
            value=f"Coins: `{t['coin_ids'] or 'none'}` | Cash: ${t['cash_offer']:.4f}\nCreated: {t['created_at'].strftime('%Y-%m-%d %H:%M')}",
            inline=False
        )
    await ctx.send(embed=e)

# ── -auction <coin_id> <start_price> [hours] ───────────────────────────────────
@bot.command()
async def auction(ctx, coin_id: int, start_price: float, hours: float = 24.0):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))

    if start_price <= 0:
        await ctx.send("❌ Start price must be greater than 0.")
        return
    if hours < 1 or hours > 168:
        await ctx.send("❌ Duration must be 1–168 hours.")
        return

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM coins WHERE id = %s AND owner_id = %s", (coin_id, uid))
    c = cur.fetchone()

    if not c:
        await ctx.send(f"❌ Coin #{coin_id} not in your inventory.")
        release(conn)
        return

    cur.execute("SELECT id FROM auctions WHERE coin_id = %s AND status = 'active'", (coin_id,))
    if cur.fetchone():
        await ctx.send(f"❌ Coin #{coin_id} is already listed.")
        release(conn)
        return

    ends_at = datetime.now(timezone.utc) + timedelta(hours=hours)
    cur.execute("""
        INSERT INTO auctions (seller_id, coin_id, start_price, ends_at)
        VALUES (%s, %s, %s, %s) RETURNING id
    """, (uid, coin_id, start_price, ends_at))
    auction_id = cur.fetchone()['id']
    conn.commit()
    release(conn)

    e = discord.Embed(title="🏪 Auction Listed!", color=0x57F287)
    e.description = coin_display(c)
    e.add_field(name="Starting Price", value=f"**${start_price:.4f}**", inline=True)
    e.add_field(name="Ends At", value=f"<t:{int(ends_at.timestamp())}:R>", inline=True)
    e.add_field(name="Fee", value="10% on final sale", inline=True)
    e.set_footer(text=f"Auction ID: #{auction_id}")
    await ctx.send(embed=e)

# ── -market [page] ────────────────────────────────────────────────────────────
@bot.command()
async def market(ctx, page: int = 1):
    per_page = 5
    offset = (page - 1) * per_page

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM auctions WHERE status = 'active'", )
    total = cur.fetchone()['c']
    cur.execute("""
        SELECT a.*, c.material, c.variant, c.status as cond, c.float, c.serial, c.value as coin_val,
               u.username as seller_name
        FROM auctions a
        JOIN coins c ON c.id = a.coin_id
        JOIN users u ON u.user_id = a.seller_id
        WHERE a.status = 'active'
        ORDER BY a.ends_at ASC
        LIMIT %s OFFSET %s
    """, (per_page, offset))
    rows = cur.fetchall()
    release(conn)

    if not rows:
        await ctx.send("🏪 No active auctions. List yours with `-auction <coin_id> <price>`")
        return

    pages = math.ceil(total / per_page)
    e = discord.Embed(title=f"🏪 Coin Marketplace — Page {page}/{pages}", color=0xEB459E)
    e.description = f"{total} active listing(s)\n"

    for a in rows:
        serial_str = str(a['serial']).zfill(4)
        tier = tier_emoji(a['coin_val'])
        top_bid = f"${a['current_bid']:.4f}" if a['current_bid'] else "No bids"
        e.add_field(
            name=f"{tier} Auction #{a['id']} — {a['variant']} {a['material']} Coin #{serial_str}",
            value=(
                f"Condition: **{a['cond']}** | Float: **{a['float']}**\n"
                f"Coin Value: **${a['coin_val']:.4f}** | Start: **${a['start_price']:.4f}** | Top Bid: **{top_bid}**\n"
                f"Seller: {a['seller_name']} | Ends: <t:{int(a['ends_at'].replace(tzinfo=timezone.utc).timestamp())}:R>"
            ),
            inline=False
        )

    e.set_footer(text="Use -bid <auction_id> to place a bid")
    await ctx.send(embed=e)

# ── -bid <auction_id> ─────────────────────────────────────────────────────────
@bot.command()
async def bid(ctx, auction_id: int):
    ensure_user(ctx.author.id, str(ctx.author))
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM auctions WHERE id = %s AND status = 'active'", (auction_id,))
    a = cur.fetchone()
    release(conn)

    if not a:
        await ctx.send(f"❌ Auction #{auction_id} not found or ended.")
        return

    view = AuctionView(auction_id)
    min_bid = max(a['start_price'], (a['current_bid'] or 0) + 0.01)
    await ctx.send(f"💰 Bidding on Auction **#{auction_id}** | Minimum bid: **${min_bid:.4f}**", view=view)

# ── -myauctions ───────────────────────────────────────────────────────────────
@bot.command()
async def myauctions(ctx):
    uid = ctx.author.id
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT a.*, c.material, c.variant, c.serial
        FROM auctions a JOIN coins c ON c.id = a.coin_id
        WHERE a.seller_id = %s AND a.status = 'active'
        ORDER BY a.ends_at ASC
    """, (uid,))
    rows = cur.fetchall()
    release(conn)

    if not rows:
        await ctx.send("📭 You have no active listings.")
        return

    e = discord.Embed(title="📋 Your Active Auctions", color=0xEB459E)
    for a in rows:
        serial_str = str(a['serial']).zfill(4)
        top_bid = f"${a['current_bid']:.4f}" if a['current_bid'] else "No bids"
        e.add_field(
            name=f"Auction #{a['id']} — {a['variant']} {a['material']} Coin #{serial_str}",
            value=f"Start: ${a['start_price']:.4f} | Top Bid: {top_bid}\nEnds: <t:{int(a['ends_at'].replace(tzinfo=timezone.utc).timestamp())}:R>",
            inline=False
        )
    await ctx.send(embed=e)

# ── -cancelauction <id> ───────────────────────────────────────────────────────
@bot.command()
async def cancelauction(ctx, auction_id: int):
    uid = ctx.author.id
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM auctions WHERE id = %s AND seller_id = %s AND status = 'active'", (auction_id, uid))
    a = cur.fetchone()

    if not a:
        await ctx.send(f"❌ Auction #{auction_id} not found or not yours.")
        release(conn)
        return

    if a['bidder_id'] and a['current_bid']:
        cur.execute("UPDATE users SET cash = cash + %s WHERE user_id = %s", (a['current_bid'], a['bidder_id']))

    cur.execute("UPDATE coins SET owner_id = %s WHERE id = %s", (uid, a['coin_id']))
    cur.execute("UPDATE auctions SET status = 'cancelled' WHERE id = %s", (auction_id,))
    conn.commit()
    release(conn)

    await ctx.send(f"✅ Auction **#{auction_id}** cancelled. Coin returned to your inventory.")

# ── -profile [@user] ──────────────────────────────────────────────────────────
@bot.command()
async def profile(ctx, member: discord.Member = None):
    target = member or ctx.author
    uid = target.id
    ensure_user(uid, str(target))

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = %s", (uid,))
    u = cur.fetchone()

    cur.execute("SELECT value FROM coins WHERE owner_id = %s ORDER BY value DESC LIMIT 1", (uid,))
    best = cur.fetchone()

    cur.execute("SELECT SUM(value) as total FROM coins WHERE owner_id = %s", (uid,))
    total_val = cur.fetchone()['total'] or 0

    cur.execute("SELECT COUNT(*) as c FROM auctions WHERE seller_id = %s AND status = 'sold'", (uid,))
    sales = cur.fetchone()['c']
    release(conn)

    e = discord.Embed(title=f"👤 {target.display_name}'s Profile", color=0x5865F2)
    e.set_thumbnail(url=target.display_avatar.url)
    e.add_field(name="🎟️ Credits", value=f"{u['credits']:,}", inline=True)
    e.add_field(name="💵 Cash", value=f"${u['cash']:.4f}", inline=True)
    e.add_field(name="🪙 Coins", value=str(u['total_coins']), inline=True)
    e.add_field(name="📈 Portfolio Value", value=f"${total_val:.4f}", inline=True)
    e.add_field(name="🏆 Best Coin", value=f"${best['value']:.4f}" if best else "None", inline=True)
    e.add_field(name="🛒 Completed Sales", value=str(sales), inline=True)
    e.set_footer(text=f"Member since {u['joined_at'].strftime('%Y-%m-%d')}")
    await ctx.send(embed=e)

# ── -leaderboard / -lb ────────────────────────────────────────────────────────
@bot.command(aliases=['lb'])
async def leaderboard(ctx):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT u.username, u.cash, u.total_coins,
               COALESCE(SUM(c.value), 0) as portfolio
        FROM users u
        LEFT JOIN coins c ON c.owner_id = u.user_id
        GROUP BY u.user_id, u.username, u.cash, u.total_coins
        ORDER BY portfolio DESC
        LIMIT 10
    """)
    rows = cur.fetchall()
    release(conn)

    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    e = discord.Embed(title="🏆 CoinVault Leaderboard — Top Collectors", color=0xFFD700)
    for i, r in enumerate(rows):
        e.add_field(
            name=f"{medals[i]} {r['username']}",
            value=f"Portfolio: **${r['portfolio']:.4f}** | Cash: **${r['cash']:.4f}** | Coins: **{r['total_coins']}**",
            inline=False
        )
    await ctx.send(embed=e)

# ── -bank ─────────────────────────────────────────────────────────────────────
@bot.command()
async def bank(ctx):
    total = get_bank()
    n = count_users()
    share = round(total / n, 4) if n > 0 else 0

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM daily_log ORDER BY paid_date DESC LIMIT 1")
    last = cur.fetchone()
    cur.execute("SELECT SUM(amount) as t FROM bank_log WHERE logged_at > NOW() - INTERVAL '24 hours'")
    day_income = cur.fetchone()['t'] or 0
    release(conn)

    e = discord.Embed(title="🏦 CoinVault Bank Treasury", color=0x57F287)
    e.add_field(name="💰 Current Balance", value=f"**${total:.4f}**", inline=True)
    e.add_field(name="👥 Registered Users", value=str(n), inline=True)
    e.add_field(name="📤 Projected Share", value=f"**${share:.4f}** per user", inline=True)
    e.add_field(name="📈 Income (24h)", value=f"${day_income:.4f}", inline=True)
    if last:
        e.add_field(name="📅 Last Payout", value=f"{last['paid_date']} — ${last['amount']:.4f}/user", inline=True)
    e.set_footer(text="Bank fills from crate purchases (5%) and trade taxes (10%)")
    await ctx.send(embed=e)

# ── -stats ────────────────────────────────────────────────────────────────────
@bot.command()
async def stats(ctx):
    uid = ctx.author.id
    ensure_user(uid, str(ctx.author))

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = %s", (uid,))
    u = cur.fetchone()
    cur.execute("SELECT COUNT(*) as c FROM trades WHERE (initiator_id = %s OR receiver_id = %s) AND status = 'completed'", (uid, uid))
    completed_trades = cur.fetchone()['c']
    cur.execute("SELECT COUNT(*) as c, SUM(value) as s FROM coins WHERE owner_id = %s", (uid,))
    coin_stats = cur.fetchone()
    cur.execute("""
        SELECT material, COUNT(*) as c FROM coins WHERE owner_id = %s GROUP BY material ORDER BY c DESC LIMIT 3
    """, (uid,))
    top_mats = cur.fetchall()
    release(conn)

    e = discord.Embed(title=f"📊 Stats — {ctx.author.display_name}", color=0x5865F2)
    e.add_field(name="🎟️ Credits", value=f"{u['credits']:,}", inline=True)
    e.add_field(name="💵 Cash", value=f"${u['cash']:.4f}", inline=True)
    e.add_field(name="🤝 Trades Done", value=str(completed_trades), inline=True)
    e.add_field(name="🪙 Coins Owned", value=str(coin_stats['c'] or 0), inline=True)
    e.add_field(name="📈 Portfolio Value", value=f"${coin_stats['s']:.4f}" if coin_stats['s'] else "$0.0000", inline=True)
    if top_mats:
        e.add_field(name="🏅 Top Materials", value="\n".join(f"{r['material']}: {r['c']}" for r in top_mats), inline=True)
    await ctx.send(embed=e)

# ─── Run ──────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
