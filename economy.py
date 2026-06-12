import discord
from discord.ext import commands
from database import get_conn, put_conn, ensure_user, get_user, get_bank, add_to_bank, log_transaction
from coin_engine import generate_coin, coin_embed_description

CRATE_CREDIT_COST = 100
CRATE_CASH_COST   = 10.0
CRATE_BANK_TAX    = 0.05   # 5% to bank on cash crate purchase

class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ─── Balance ─────────────────────────────────────────────────────────────
    @commands.command(aliases=['bal', 'balance'])
    async def wallet(self, ctx):
        ensure_user(ctx.author.id, ctx.author.name)
        user = get_user(ctx.author.id)
        embed = discord.Embed(title=f"👛 {ctx.author.display_name}'s Wallet", color=discord.Color.green())
        embed.add_field(name="🎫 Credits", value=f"`{user['credits']:,}`", inline=True)
        embed.add_field(name="💵 Cash",    value=f"`${user['cash']:.2f}`", inline=True)
        await ctx.send(embed=embed)

    # ─── Daily reward ─────────────────────────────────────────────────────────
    @commands.command()
    async def daily(self, ctx):
        ensure_user(ctx.author.id, ctx.author.name)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT tx_id, created_at FROM transactions
                    WHERE user_id = %s AND type = 'daily'
                    ORDER BY created_at DESC LIMIT 1
                """, (ctx.author.id,))
                last = cur.fetchone()
                from datetime import datetime, timezone, timedelta
                now = datetime.now(timezone.utc)
                if last and (now - last['created_at']) < timedelta(hours=20):
                    remaining = timedelta(hours=20) - (now - last['created_at'])
                    h, m = divmod(int(remaining.total_seconds()), 3600)
                    m //= 60
                    await ctx.send(f"⏳ Daily already claimed! Come back in **{h}h {m}m**.")
                    return
                reward_cash = 5.0
                cur.execute("UPDATE users SET cash = cash + %s WHERE user_id = %s", (reward_cash, ctx.author.id))
                log_transaction(ctx.author.id, 'daily', reward_cash, "Daily reward claimed")
            conn.commit()
        finally:
            put_conn(conn)
        embed = discord.Embed(title="🎁 Daily Reward!", description=f"You received **$5.00** cash!", color=discord.Color.gold())
        await ctx.send(embed=embed)

    # ─── Buy crate with credits ───────────────────────────────────────────────
    @commands.command(aliases=['buycrate', 'open'])
    async def crate(self, ctx, method: str = "credits"):
        ensure_user(ctx.author.id, ctx.author.name)
        user = get_user(ctx.author.id)

        if method.lower() in ('cash', '$'):
            if user['cash'] < CRATE_CASH_COST:
                await ctx.send(f"❌ Not enough cash! A crate costs **${CRATE_CASH_COST:.2f}**. You have **${user['cash']:.2f}**.")
                return
            tax = round(CRATE_CASH_COST * CRATE_BANK_TAX, 4)
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET cash = cash - %s WHERE user_id = %s", (CRATE_CASH_COST, ctx.author.id))
                    cur.execute("UPDATE bank SET balance = balance + %s WHERE id = 1", (tax,))
                conn.commit()
            finally:
                put_conn(conn)
            log_transaction(ctx.author.id, 'crate_cash', -CRATE_CASH_COST, f"Opened cash crate (5% tax: ${tax:.4f} → bank)")
        else:
            if user['credits'] < CRATE_CREDIT_COST:
                await ctx.send(f"❌ Not enough credits! A crate costs **{CRATE_CREDIT_COST} credits**. You have **{user['credits']}**.")
                return
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET credits = credits - %s WHERE user_id = %s", (CRATE_CREDIT_COST, ctx.author.id))
                conn.commit()
            finally:
                put_conn(conn)
            log_transaction(ctx.author.id, 'crate_credits', -CRATE_CREDIT_COST, "Opened credit crate")

        # Generate and save coin
        coin = generate_coin()
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO coins (owner_id, material, variant, status, float, serial, raw_value, final_value)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING coin_id
                """, (ctx.author.id, coin['material'], coin['variant'], coin['status'],
                      coin['float'], coin['serial'], coin['raw_value'], coin['final_value']))
                coin_id = cur.fetchone()['coin_id']
            conn.commit()
        finally:
            put_conn(conn)

        embed = discord.Embed(
            title="📦 Crate Opened!",
            description=f"You received a coin! (ID: **#{coin_id}**)\n\n{coin_embed_description(coin)}",
            color=discord.Color.gold()
        )
        embed.set_footer(text="Use -inventory to view all your coins.")
        await ctx.send(embed=embed)

    # ─── Bank info ────────────────────────────────────────────────────────────
    @commands.command()
    async def bank(self, ctx):
        b = get_bank()
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) as cnt FROM users")
                cnt = cur.fetchone()['cnt']
        finally:
            put_conn(conn)

        share = round(b['balance'] / cnt, 4) if cnt > 0 else 0
        last_dist = b['last_distributed'].strftime("%Y-%m-%d %H:%M UTC") if b['last_distributed'] else "Never"
        embed = discord.Embed(title="🏦 Community Bank", color=discord.Color.blurple())
        embed.add_field(name="💰 Balance",          value=f"${b['balance']:.4f}", inline=True)
        embed.add_field(name="👥 Users",            value=str(cnt), inline=True)
        embed.add_field(name="📊 Est. Share/User",  value=f"${share:.4f}", inline=True)
        embed.add_field(name="🕐 Last Distributed", value=last_dist, inline=False)
        embed.set_footer(text="Bank collects 5% on crate purchases + 10% on trades. Distributed daily to all users.")
        await ctx.send(embed=embed)

    # ─── Help ─────────────────────────────────────────────────────────────────
    @commands.command()
    async def help(self, ctx):
        embed = discord.Embed(title="📜 CoinBot Help", color=discord.Color.blue())
        embed.add_field(name="💰 Economy", value=(
            "`-wallet` / `-bal` — Your credits & cash\n"
            "`-daily` — Claim $5 daily cash reward\n"
            "`-crate` — Open crate with 100 credits\n"
            "`-crate cash` — Open crate with $10 cash (5% → bank)\n"
            "`-bank` — View community bank status"
        ), inline=False)
        embed.add_field(name="🪙 Coins", value=(
            "`-inventory` / `-inv` — View your coins\n"
            "`-coin <id>` — Inspect a coin's details\n"
            "`-mycoins` — Quick list of coins with values"
        ), inline=False)
        embed.add_field(name="🤝 Trading", value=(
            "`-trade @user <your_coin_id> [their_coin_id] [cash]` — Send trade offer\n"
            "`-trades` — View pending trades\n"
            "`-accept <trade_id>` — Accept a trade\n"
            "`-decline <trade_id>` — Decline a trade\n"
            "_10% tax on all trades goes to bank_"
        ), inline=False)
        embed.add_field(name="🏪 Marketplace", value=(
            "`-sell <coin_id> <starting_price> [hours]` — List coin at auction\n"
            "`-bid <listing_id> <amount>` — Place a bid\n"
            "`-market` — View active listings\n"
            "`-cancel <listing_id>` — Cancel your listing"
        ), inline=False)
        embed.add_field(name="👤 Profile & Ranks", value=(
            "`-profile [@user]` — View player profile\n"
            "`-leaderboard` / `-lb` — Top players by coin value\n"
            "`-credits_lb` — Top by credits"
        ), inline=False)
        embed.set_footer(text="Earn 1 credit per message sent! 100 credits = 1 crate.")
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Economy(bot))
