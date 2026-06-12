import discord
from discord.ext import commands
from datetime import datetime, timezone, timedelta
from database import get_conn, put_conn, ensure_user, get_user, log_transaction

class Marketplace(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(aliases=['sell', 'auction'])
    async def market_sell(self, ctx, coin_id: int, starting_price: float, hours: int = 24):
        """List a coin for auction: -sell <coin_id> <price> [hours=24]"""
        if starting_price <= 0:
            await ctx.send("❌ Starting price must be greater than $0.")
            return
        if hours < 1 or hours > 168:
            await ctx.send("❌ Auction duration must be between 1 and 168 hours (7 days).")
            return

        ensure_user(ctx.author.id, ctx.author.name)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT * FROM coins WHERE coin_id = %s AND owner_id = %s 
                    AND for_sale = FALSE AND trade_locked = FALSE
                """, (coin_id, ctx.author.id))
                coin = cur.fetchone()
                if not coin:
                    await ctx.send(f"❌ You don't own coin #{coin_id} or it's already listed/locked.")
                    put_conn(conn)
                    return

                ends_at = datetime.now(timezone.utc) + timedelta(hours=hours)
                cur.execute("UPDATE coins SET for_sale = TRUE WHERE coin_id = %s", (coin_id,))
                cur.execute("""
                    INSERT INTO marketplace (seller_id, coin_id, starting_price, current_bid, ends_at)
                    VALUES (%s, %s, %s, %s, %s) RETURNING listing_id
                """, (ctx.author.id, coin_id, starting_price, 0.0, ends_at))
                listing_id = cur.fetchone()['listing_id']
            conn.commit()
        finally:
            put_conn(conn)

        embed = discord.Embed(title="🏪 Coin Listed!", color=discord.Color.green())
        embed.add_field(name="🪙 Coin",          value=f"#{coin_id} — {coin['variant']} {coin['material']}", inline=True)
        embed.add_field(name="💵 Starting Price", value=f"${starting_price:.2f}", inline=True)
        embed.add_field(name="⏱️ Duration",       value=f"{hours}h", inline=True)
        embed.add_field(name="🔖 Listing ID",     value=f"#{listing_id}", inline=True)
        embed.set_footer(text="10% of final sale goes to the bank.")
        await ctx.send(embed=embed)

    @commands.command()
    async def market(self, ctx, page: int = 1):
        """View active marketplace listings."""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT m.*, c.material, c.variant, c.status, c.float, c.serial, c.final_value,
                           u.username AS seller_name,
                           b.username AS bidder_name
                    FROM marketplace m
                    JOIN coins c ON c.coin_id = m.coin_id
                    JOIN users u ON u.user_id = m.seller_id
                    LEFT JOIN users b ON b.user_id = m.highest_bidder
                    WHERE m.status = 'active'
                    ORDER BY m.ends_at ASC
                    LIMIT 10 OFFSET %s
                """, ((page - 1) * 10,))
                listings = cur.fetchall()
                cur.execute("SELECT COUNT(*) AS cnt FROM marketplace WHERE status = 'active'")
                total = cur.fetchone()['cnt']
        finally:
            put_conn(conn)

        if not listings:
            await ctx.send("🏪 No active listings right now.")
            return

        embed = discord.Embed(title=f"🏪 Marketplace — Page {page}", color=discord.Color.blurple())
        for l in listings:
            current = l['current_bid'] if l['current_bid'] > 0 else l['starting_price']
            bidder = f"(top: {l['bidder_name']})" if l['bidder_name'] else "(no bids)"
            time_left = l['ends_at'] - datetime.now(timezone.utc)
            h, rem = divmod(int(time_left.total_seconds()), 3600)
            m = rem // 60
            embed.add_field(
                name=f"#{l['listing_id']} — {l['variant']} {l['material']} (${l['final_value']:.2f} value)",
                value=f"Seller: {l['seller_name']} | Coin #{l['coin_id']}\n"
                      f"💵 Current: **${current:.2f}** {bidder} | ⏱️ Ends in: {h}h {m}m",
                inline=False
            )

        embed.set_footer(text=f"Total listings: {total} | Use -bid <listing_id> <amount>")
        await ctx.send(embed=embed)

    @commands.command()
    async def bid(self, ctx, listing_id: int, amount: float):
        """Place a bid on an auction listing."""
        ensure_user(ctx.author.id, ctx.author.name)
        user = get_user(ctx.author.id)

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT * FROM marketplace WHERE listing_id = %s AND status = 'active' AND ends_at > NOW()
                """, (listing_id,))
                listing = cur.fetchone()

                if not listing:
                    await ctx.send("❌ Listing not found or already ended.")
                    put_conn(conn)
                    return
                if listing['seller_id'] == ctx.author.id:
                    await ctx.send("❌ You can't bid on your own listing.")
                    put_conn(conn)
                    return

                min_bid = max(listing['starting_price'], listing['current_bid'] + 0.01)
                if amount < min_bid:
                    await ctx.send(f"❌ Minimum bid is **${min_bid:.2f}**.")
                    put_conn(conn)
                    return
                if user['cash'] < amount:
                    await ctx.send(f"❌ Not enough cash. You have **${user['cash']:.2f}**.")
                    put_conn(conn)
                    return

                # Refund previous highest bidder
                if listing['highest_bidder']:
                    cur.execute("UPDATE users SET cash = cash + %s WHERE user_id = %s",
                                (listing['current_bid'], listing['highest_bidder']))
                    log_transaction(listing['highest_bidder'], 'bid_refund', listing['current_bid'],
                                    f"Outbid on listing #{listing_id}")

                # Deduct from new bidder
                cur.execute("UPDATE users SET cash = cash - %s WHERE user_id = %s", (amount, ctx.author.id))
                cur.execute("""
                    UPDATE marketplace SET current_bid = %s, highest_bidder = %s 
                    WHERE listing_id = %s
                """, (amount, ctx.author.id, listing_id))
                log_transaction(ctx.author.id, 'bid_placed', -amount, f"Bid ${amount:.2f} on listing #{listing_id}")
            conn.commit()
        finally:
            put_conn(conn)

        await ctx.send(f"✅ Bid of **${amount:.2f}** placed on listing **#{listing_id}**!")

    @commands.command()
    async def cancel(self, ctx, listing_id: int):
        """Cancel your marketplace listing (only if no bids)."""
        ensure_user(ctx.author.id, ctx.author.name)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT * FROM marketplace WHERE listing_id = %s AND seller_id = %s AND status = 'active'
                """, (listing_id, ctx.author.id))
                listing = cur.fetchone()
                if not listing:
                    await ctx.send("❌ Listing not found or not yours.")
                    put_conn(conn)
                    return
                if listing['current_bid'] > 0:
                    await ctx.send("❌ You can't cancel after bids have been placed.")
                    put_conn(conn)
                    return

                cur.execute("UPDATE coins SET for_sale = FALSE WHERE coin_id = %s", (listing['coin_id'],))
                cur.execute("UPDATE marketplace SET status = 'cancelled' WHERE listing_id = %s", (listing_id,))
            conn.commit()
        finally:
            put_conn(conn)

        await ctx.send(f"✅ Listing **#{listing_id}** cancelled. Coin returned.")

    @commands.command()
    async def mylistings(self, ctx):
        """View your active marketplace listings."""
        ensure_user(ctx.author.id, ctx.author.name)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT m.*, c.material, c.variant, c.final_value
                    FROM marketplace m JOIN coins c ON c.coin_id = m.coin_id
                    WHERE m.seller_id = %s AND m.status = 'active'
                """, (ctx.author.id,))
                listings = cur.fetchall()
        finally:
            put_conn(conn)

        if not listings:
            await ctx.send("📭 You have no active listings.")
            return

        embed = discord.Embed(title="📦 Your Listings", color=discord.Color.green())
        for l in listings:
            time_left = l['ends_at'] - datetime.now(timezone.utc)
            h = int(time_left.total_seconds() // 3600)
            embed.add_field(
                name=f"#{l['listing_id']} — {l['variant']} {l['material']}",
                value=f"Starting: ${l['starting_price']:.2f} | Current: ${l['current_bid']:.2f} | Ends in: {h}h",
                inline=False
            )
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Marketplace(bot))
