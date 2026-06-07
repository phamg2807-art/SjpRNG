
import discord
from discord.ext import commands

# Bot configuration
# Ensure you use the Token from the "Bot" tab in your Developer Portal
# NOT the Client Secret provided in your message.
TOKEN = 'YOUR_BOT_TOKEN_HERE' 

# Set up intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})')
    print('------')

@bot.command()
async def changecolor(ctx, role_name: str, color_hex: str):
    """
    Usage: !changecolor "Role Name" 0xFF0000
    Changes a role's color using a hex code.
    """
    # Convert hex string (e.g., 'FF0000') to discord.Color
    try:
        color = discord.Color(int(color_hex.replace('0x', ''), 16))
    except ValueError:
        await ctx.send("Invalid color format. Please use hex (e.g., 0xFF0000).")
        return

    role = discord.utils.get(ctx.guild.roles, name=role_name)
    
    if role:
        try:
            await role.edit(color=color)
            await ctx.send(f'Successfully changed {role.name} color!')
        except discord.Forbidden:
            await ctx.send("I don't have permission to edit that role. Check my role hierarchy.")
    else:
        await ctx.send(f"Could not find a role named '{role_name}'.")

@bot.command()
async def ping(ctx):
    await ctx.send('Pong!')

# Run the bot
bot.run(TOKEN)
