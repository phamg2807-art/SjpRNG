async def fish_action(interaction):
    user = interaction.user
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM players WHERE user_id = %s", (user.id,))
        player = cur.fetchone()
        if not player:
            await interaction.response.send_message("You need to join the game first! Use the Join Game button.", ephemeral=True)
            return

        # Cooldown (30 seconds)
        if player['last_fish_time']:
            cooldown = (time.time() - player['last_fish_time'].timestamp())
            if cooldown < 30:
                await interaction.response.send_message(
                    f"⏳ Please wait {int(30 - cooldown)} seconds.", ephemeral=True
                )
                return

        # --- Start fishing animation ---
        await interaction.response.send_message("🎣 Casting your line...")
        msg = await interaction.original_response()

        await asyncio.sleep(1.5)
        await msg.edit(content="🌊 Waiting for a bite...")

        await asyncio.sleep(2)
        await msg.edit(content="🐟 Something's pulling!")

        await asyncio.sleep(1)
        await msg.edit(content="🎣 Reeling it in...")

        await asyncio.sleep(1.5)
        # --- End animation, now compute the catch ---

        location_key = player['current_location']
        loc_data = LOCATIONS.get(location_key)
        if not loc_data:
            location_key = '1-fisher-shore'
            loc_data = LOCATIONS[location_key]

        pool = get_fish_for_location(location_key)
        if not pool:
            await msg.edit(content="❌ No fish available in this location.")
            return

        # Select fish weighted by rarity chance
        weights = [RARITIES[f['rarity']][2] for f in pool]
        chosen = random.choices(pool, weights=weights, k=1)[0]

        # Determine weight and depth
        weight = random.uniform(chosen['weight_min'], chosen['weight_max'])
        depth = random.uniform(chosen['depth_min'], chosen['depth_max'])

        # Roll for mutation
        mutation = roll_mutation()

        # Calculate price
        price = calculate_price(chosen, weight, mutation)

        # Insert into caught_fish
        cur.execute("""
            INSERT INTO caught_fish (user_id, fish_name, weight, rarity, mutation, base_price, final_price, location)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (user.id, chosen['name'], weight, chosen['rarity'], mutation, price, price, location_key))

        # Update player stats
        cur.execute("""
            UPDATE players
            SET fish_caught = fish_caught + 1,
                coins = coins + %s,
                experience = experience + %s,
                last_fish_time = CURRENT_TIMESTAMP
            WHERE user_id = %s
        """, (price, price // 10, user.id))
        conn.commit()

        # Build final embed
        embed = discord.Embed(
            title="🎣 You caught a fish!",
            color=discord.Color.gold()
        )
        embed.add_field(name="Fish", value=f"**{chosen['name']}**", inline=True)
        embed.add_field(name="Weight", value=f"{weight:.2f} kg", inline=True)
        embed.add_field(name="Rarity", value=f"⭐ {chosen['rarity']}", inline=True)
        if mutation:
            embed.add_field(name="Mutation", value=f"✨ {mutation} (x{MUTATIONS[mutation][0]} value)", inline=True)
        embed.add_field(name="Value", value=f"💰 {price} coins", inline=True)
        embed.add_field(name="Depth", value=f"{depth:.1f}m", inline=True)
        embed.set_footer(text=f"Location: {loc_data['name']}")

        # Replace the animation message with the result
        await msg.edit(content=None, embed=embed)

    except Exception as e:
        # If something fails, try to send error (if we already sent a message, edit it)
        try:
            await msg.edit(content=f"❌ Error: {e}")
        except:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        conn.rollback()
    finally:
        release(conn)
    
