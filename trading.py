import discord
from discord.ext import commands
from database import get_conn, put_conn, ensure_user, get_user, add_to_bank, log_transaction

TRADE_TAX = 0.10  # 10% fee to bank

class Trading(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # -trade @user <your_coin_id> [their_coin_id] [cash_offer]
    @commands.command()
    async def trade(self, ctx, member: discord.Member, your_coin_id: int,
                    their_coin_id: int = None, cash_offer: float = 0.0):
        if member.id == ctx.author.id:
            await ctx.send("❌ You can't trade with yourself.")
            return
        if cash_offer < 0:
            await ctx.send("❌ Cash offer can't be negative.")
            return

        ensure_user(ctx.author.id, ctx.author.name)
        ensure_user(member.id, member.name)

        sender = get_user(ctx.author.id)
        if cash_offer > 0 and sender['cash'] < cash_offer:
            await ctx.send(f"❌ You don't have ${cash_offer:.2f} cash for this offer.")
            return

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # Validate sender coin
                cur.execute("SELECT * FROM coins WHERE coin_id = %s AND owner_id = %s AND for_sale = FALSE AND trade_locked = FALSE",
                            (your_coin_id, ctx.author.id))
                your_coin = cur.fetchone()
                if not your_coin:
                    await ctx.send(f"❌ You don't own coin #{your_coin_id} or it's listed/locked.")
                    put_conn(conn)
                    return

                # Validate receiver coin if specified
                their_coin = None
                if their_coin_id:
                    cur.execute("SELECT * FROM coins WHERE coin_id = %s AND owner_id = %s AND for_sale = FALSE AND trade_locked = FALSE",
                                (their_coin_id, member.id))
                    their_coin = cur.fetchone()
                    if not their_coin:
                        await ctx.send(f"❌ {member.display_name} doesn't own coin #{their_coin_id} or it's not available.")
                        put_conn(conn)
                        return

                # Lock coins in trade
                cur.execute("UPDATE coins SET trade_locked = TRUE WHERE coin_id = %s", (your_coin_id,))
                if their_coin_id:
                    cur.execute("UPDATE coins SET trade_locked = TRUE WHERE coin_id = %s", (their_coin_id,))

                # Reserve cash
                if cash_offer > 0:
                    cur.execute("UPDATE users SET cash = cash - %s WHERE user_id = %s", (cash_offer, ctx.author.id))

                cur.execute("""
                    INSERT INTO trades (sender_id, receiver_id, sender_coin_id, receiver_coin_id, cash_offer)
                    VALUES (%s, %s, %s, %s, %s) RETURNING trade_id
                """, (ctx.author.id, member.id, your_coin_id, their_coin_id, cash_offer))
                trade_id = cur.fetchone()['trade_id']
            conn.commit()
        finally:
            put_conn(conn)

        # Build offer description
        offer_parts = [f"Coin **#{your_coin_id}** ({your_coin['material']} {your_coin['variant']}, ${your_coin['final_value']:.2f})"]
        if cash_offer > 0:
            offer_parts.append(f"**${cash_offer:.2f}** cash")

        want_parts = []
        if their_coin:
            want_parts.append(f"Coin **#{their_coin_id}** ({their_coin['material']} {their_coin['variant']}, ${their_coin['final_value']:.2f})")
        else:
            want_parts.append("Nothing (gift)")

        embed = discord.Embed(
            title=f"🤝 Trade Offer #{trade_id}",
            description=f"**{ctx.author.display_name}** → **{member.display_name}**",
            color=discord.Color.orange()
        )
        embed.add_field(name="📤 Offering", value="\n".join(offer_parts), inline=True)
        embed.add_field(name="📥 Wants",    value="\n".join(want_parts), inline=True)
        embed.add_field(name="⚠️ Note",     value="10% tax on cash value applied to bank on acceptance.", inline=False)
        embed.set_footer(text=f"Trade ID: #{trade_id} | {member.display_name}: use -accept {trade_id} or -decline {trade_id}")

        await ctx.send(f"{member.mention}", embed=embed)

    @commands.command()
    async def trades(self, ctx):
        ensure_user(ctx.author.id, ctx.author.name)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT t.*, 
                        uc.material AS s_mat, uc.variant AS s_var, uc.final_value AS s_val,
                        us.username AS sender_name
                    FROM trades t
                    JOIN coins uc ON uc.coin_id = t.sender_coin_id
                    JOIN users us ON us.user_id = t.sender_id
                    WHERE t.receiver_id = %s AND t.status = 'pending'
                    ORDER BY t.created_at DESC
                """, (ctx.author.id,))
                incoming = cur.fetchall()

                cur.execute("""
                    SELECT t.*, us.username AS receiver_name
                    FROM trades t
                    JOIN users us ON us.user_id = t.receiver_id
                    WHERE t.sender_id = %s AND t.status = 'pending'
                    ORDER BY t.created_at DESC
                """, (ctx.author.id,))
                outgoing = cur.fetchall()
        finally:
            put_conn(conn)

        embed = discord.Embed(title="🤝 Your Trades", color=discord.Color.orange())

        if incoming:
            lines = []
            for t in incoming:
                cash_str = f" + ${t['cash_offer']:.2f}" if t['cash_offer'] > 0 else ""
                lines.append(f"`#{t['trade_id']}` from **{t['sender_name']}** — Coin #{t['sender_coin_id']} ({t['s_mat']} {t['s_var']}, ${t['s_val']:.2f}){cash_str}")
            embed.add_field(name="📥 Incoming", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="📥 Incoming", value="No incoming trades.", inline=False)

        if outgoing:
            lines = [f"`#{t['trade_id']}` → **{t['receiver_name']}** (pending)" for t in outgoing]
            embed.add_field(name="📤 Outgoing", value="\n".join(lines), inline=False)

        embed.set_footer(text="Use -accept <id> or -decline <id>")
        await ctx.send(embed=embed)

    @commands.command()
    async def accept(self, ctx, trade_id: int):
        ensure_user(ctx.author.id, ctx.author.name)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM trades WHERE trade_id = %s AND receiver_id = %s AND status = 'pending'",
                            (trade_id, ctx.author.id))
                trade = cur.fetchone()
                if not trade:
                    await ctx.send("❌ Trade not found or not yours to accept.")
                    put_conn(conn)
                    return

                # Swap coins
                cur.execute("UPDATE coins SET owner_id = %s, trade_locked = FALSE WHERE coin_id = %s",
                            (ctx.author.id, trade['sender_coin_id']))
                if trade['receiver_coin_id']:
                    cur.execute("UPDATE coins SET owner_id = %s, trade_locked = FALSE WHERE coin_id = %s",
                                (trade['sender_id'], trade['receiver_coin_id']))

                # Handle cash with 10% tax
                cash = trade['cash_offer']
                if cash > 0:
                    tax = round(cash * TRADE_TAX, 4)
                    receiver_gets = round(cash - tax, 4)
                    # Cash was already deducted from sender; give receiver their cut
                    cur.execute("UPDATE users SET cash = cash + %s WHERE user_id = %s", (receiver_gets, ctx.author.id))
                    cur.execute("UPDATE bank SET balance = balance + %s WHERE id = 1", (tax,))
                    log_transaction(ctx.author.id, 'trade_tax', tax, f"Received trade #{trade_id} (10% tax: ${tax:.4f} → bank)")
                    log_transaction(trade['sender_id'], 'trade_sent', -cash, f"Sent trade #{trade_id} with ${cash:.2f} cash offer")

                cur.execute("UPDATE trades SET status = 'accepted' WHERE trade_id = %s", (trade_id,))
            conn.commit()
        finally:
            put_conn(conn)

        await ctx.send(f"✅ Trade **#{trade_id}** accepted! Coins have been swapped.")

    @commands.command()
    async def decline(self, ctx, trade_id: int):
        ensure_user(ctx.author.id, ctx.author.name)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT * FROM trades WHERE trade_id = %s 
                    AND (receiver_id = %s OR sender_id = %s) AND status = 'pending'
                """, (trade_id, ctx.author.id, ctx.author.id))
                trade = cur.fetchone()
                if not trade:
                    await ctx.send("❌ Trade not found.")
                    put_conn(conn)
                    return

                # Unlock coins
                cur.execute("UPDATE coins SET trade_locked = FALSE WHERE coin_id = %s", (trade['sender_coin_id'],))
                if trade['receiver_coin_id']:
                    cur.execute("UPDATE coins SET trade_locked = FALSE WHERE coin_id = %s", (trade['receiver_coin_id'],))

                # Refund cash if sender cancelled
                if trade['cash_offer'] > 0:
                    cur.execute("UPDATE users SET cash = cash + %s WHERE user_id = %s",
                                (trade['cash_offer'], trade['sender_id']))

                cur.execute("UPDATE trades SET status = 'declined' WHERE trade_id = %s", (trade_id,))
            conn.commit()
        finally:
            put_conn(conn)

        await ctx.send(f"❌ Trade **#{trade_id}** declined. Coins and cash returned.")

async def setup(bot):
    await bot.add_cog(Trading(bot))
