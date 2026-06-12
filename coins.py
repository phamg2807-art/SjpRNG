import discord
from discord.ext import commands
from database import get_conn, put_conn, ensure_user
from coin_engine import coin_embed_description, SERIAL_MULT_LABELS

TIER_COLORS = {
    "Plasma":   0xFF00FF,
    "Amethyst": 0x9B59B6,
    "Diamond":  0x00FFFF,
    "Gold":     0xFFD700,
    "Obsidian": 0x2C3E50,
    "Carbon":   0x555555,
    "default":  0x5865F2,
}

def coin_color(material):
    return TIER_COLORS.get(material, TIER_COLORS["default"])

class Coins(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(aliases=['inv', 'inventory'])
    async def mycoins(self, ctx, member: discord.Member = None):
        target = member or ctx.author
        ensure_user(target.id, target.name)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT coin_id, material, variant, status, float, serial, final_value, for_sale
                    FROM coins WHERE owner_id = %s ORDER BY final_value DESC
                """, (target.id,))
                coins = cur.fetchall()
        finally:
            put_conn(conn)

        if not coins:
            await ctx.send(f"🪙 **{target.display_name}** has no coins yet! Open a crate with `-crate`.")
            return

        lines = []
        total = 0.0
        for c in coins:
            sale_tag = " 🏪" if c['for_sale'] else ""
            lines.append(
                f"`#{c['coin_id']:04d}` **{c['variant']} {c['material']}** | "
                f"{c['status']} | {c['float']} | Serial #{c['serial']:04d} — "
                f"**${c['final_value']:.2f}**{sale_tag}"
            )
            total += c['final_value']

        # Paginate at 15 per embed
        pages = [lines[i:i+15] for i in range(0, len(lines), 15)]
        embed = discord.Embed(
            title=f"🪙 {target.display_name}'s Coin Collection",
            description="\n".join(pages[0]),
            color=discord.Color.blurple()
        )
        embed.set_footer(text=f"Total coins: {len(coins)} | Collection value: ${total:.2f} | Page 1/{len(pages)}")
        await ctx.send(embed=embed)

    @commands.command()
    async def coin(self, ctx, coin_id: int):
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT c.*, u.username 
                    FROM coins c JOIN users u ON c.owner_id = u.user_id
                    WHERE c.coin_id = %s
                """, (coin_id,))
                c = cur.fetchone()
        finally:
            put_conn(conn)

        if not c:
            await ctx.send("❌ Coin not found.")
            return

        serial_mult = 1.0
        s = c['serial']
        if s in (99, 999, 9999) or s in [x*1000 for x in range(1,10)]:
            serial_mult = 10.0
        elif s < 10:  serial_mult = 5.0
        elif s < 100: serial_mult = 3.0
        elif s % 100 == 0 or s % 500 == 0: serial_mult = 2.0

        serial_label = SERIAL_MULT_LABELS.get(serial_mult, "Normal Serial")

        embed = discord.Embed(
            title=f"🪙 Coin #{coin_id:04d}",
            color=coin_color(c['material'])
        )
        embed.add_field(name="👤 Owner",     value=c['username'], inline=True)
        embed.add_field(name="🔩 Material",  value=f"{c['material']}", inline=True)
        embed.add_field(name="🎨 Variant",   value=c['variant'], inline=True)
        embed.add_field(name="📋 Status",    value=c['status'], inline=True)
        embed.add_field(name="✨ Float",     value=c['float'], inline=True)
        embed.add_field(name="🔢 Serial",    value=f"#{c['serial']:04d} ({serial_label})", inline=True)
        embed.add_field(name="💵 Raw Value", value=f"${c['raw_value']:.2f}", inline=True)
        embed.add_field(name="💰 Final Value", value=f"**${c['final_value']:.2f}**", inline=True)
        if c['for_sale']:
            embed.add_field(name="🏪 Status", value="Listed on Marketplace", inline=True)
        embed.set_footer(text=f"Minted: {c['created_at'].strftime('%Y-%m-%d')}")
        await ctx.send(embed=embed)

    @commands.command()
    async def inspect(self, ctx, coin_id: int):
        """Detailed breakdown of a coin's multipliers."""
        await self.coin(ctx, coin_id)

async def setup(bot):
    await bot.add_cog(Coins(bot))
