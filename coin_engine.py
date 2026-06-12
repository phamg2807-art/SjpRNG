import random

# ─── Coin Attribute Tables ────────────────────────────────────────────────────

MATERIALS = [
    ("Plastic",   0.5,   10),
    ("Wood",      0.75,   8),
    ("Stone",     1.0,    2),   # 1/2 expressed as weight 25 below
    ("Bronze",    1.2,    8),
    ("Copper",    1.25,   9),
    ("Iron",      1.5,   12),
    ("Steel",     1.6,   14),
    ("Aluminum",  1.75,  18),
    ("Carbon",    2.0,   20),
    ("Tungsten",  2.25,  25),
    ("Topaz",     2.3,   25),
    ("Obsidian",  2.5,   30),
    ("Gold",      3.0,   30),
    ("Diamond",   5.0,  100),
    ("Amethyst",  10.0, 250),
    ("Plasma",    100.0,2500),
]

VARIANTS = [
    ("Brown",     0.75,   4),
    ("Gray",      1.0,    2),   # 1/2
    ("Blue",      1.5,    6),
    ("Yellow",    1.75,  10),
    ("Black",     1.8,   12),
    ("White",     2.0,   15),
    ("Rainbow",   5.0,   50),
    ("Prismatic", 10.0, 200),
]

STATUSES = [
    ("Broken",    0.5,   12),
    ("Crushed",   0.6,   10),
    ("Oxidized",  0.75,   9),
    ("Scratched", 0.8,    8),
    ("Old",       0.9,    8),
    ("Like New",  0.95,   7),
    ("Normal",    1.0,    2),   # 1/2
    ("New",       1.25,   4),
    ("Sleek",     1.5,    8),
    ("Shiny",     1.75,  10),
    ("Modern",    2.0,   15),
    ("Elegant",   2.5,   20),
    ("Stunning",  2.75,  25),
]

FLOATS = [
    ("Bad",      0.5,   15),
    ("Good",     1.0,    2),   # 1/2
    ("Great",    2.0,    4),
    ("Amazing",  3.0,    8),
    ("Heavenly", 15.0,  50),
    ("Godlike",  30.0, 100),
]

# ─── Weighted random picker ───────────────────────────────────────────────────
def weighted_pick(table):
    """Pick from table of (name, multiplier, denominator). Weight = 1/denominator."""
    entries = [(name, mult, 1.0 / denom) for name, mult, denom in table]
    total = sum(w for _, _, w in entries)
    r = random.uniform(0, total)
    cumulative = 0
    for name, mult, w in entries:
        cumulative += w
        if r <= cumulative:
            return name, mult
    return entries[-1][0], entries[-1][1]

# ─── Serial generation ────────────────────────────────────────────────────────
def roll_serial():
    return random.randint(0, 9999)

def get_serial_multiplier(serial: int) -> float:
    s = serial
    # Priority: most valuable first
    if s in (99, 999, 9999):
        return 10.0
    if s in [x * 1000 for x in range(1, 10)]:  # 1000,2000,...,9000
        return 10.0
    if s < 10:
        return 5.0
    if s < 100:
        return 3.0
    # Round serial: divisible by 100 or 500 or 1000 variants
    if s % 100 == 0 or s % 500 == 0 or s % 340 == 0 or s % 120 == 0:
        return 2.0
    return 1.0

SERIAL_MULT_LABELS = {
    10.0: "🔟 Ultra Serial",
    5.0:  "5️⃣ Rare Serial",
    3.0:  "3️⃣ Lucky Serial",
    2.0:  "2️⃣ Round Serial",
    1.0:  "Normal Serial",
}

# ─── Coin Generator ───────────────────────────────────────────────────────────
def generate_coin() -> dict:
    material, mat_mult   = weighted_pick(MATERIALS)
    variant,  var_mult   = weighted_pick(VARIANTS)
    status,   stat_mult  = weighted_pick(STATUSES)
    float_,   float_mult = weighted_pick(FLOATS)
    serial = roll_serial()
    serial_mult = get_serial_multiplier(serial)

    raw_value = round(random.uniform(1.0, 5.0), 2)
    final_value = round(raw_value * mat_mult * var_mult * stat_mult * float_mult * serial_mult, 2)

    return {
        "material":    material,
        "variant":     variant,
        "status":      status,
        "float":       float_,
        "serial":      serial,
        "raw_value":   raw_value,
        "final_value": final_value,
        # multipliers for display
        "mat_mult":    mat_mult,
        "var_mult":    var_mult,
        "stat_mult":   stat_mult,
        "float_mult":  float_mult,
        "serial_mult": serial_mult,
    }

def coin_embed_description(coin: dict) -> str:
    serial_label = SERIAL_MULT_LABELS.get(coin.get("serial_mult", 1.0), "Normal Serial")
    lines = [
        f"🔩 **Material:** {coin['material']}  ×{coin['mat_mult']}",
        f"🎨 **Variant:**  {coin['variant']}   ×{coin['var_mult']}",
        f"📋 **Status:**   {coin['status']}    ×{coin['stat_mult']}",
        f"✨ **Float:**    {coin['float']}     ×{coin['float_mult']}",
        f"🔢 **Serial:**   #{coin['serial']:04d}  ({serial_label}) ×{coin['serial_mult']}",
        "",
        f"💵 **Raw Value:** ${coin['raw_value']:.2f}",
        f"💰 **Final Value:** **${coin['final_value']:.2f}**",
    ]
    return "\n".join(lines)
