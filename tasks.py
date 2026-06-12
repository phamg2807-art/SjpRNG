from discord.ext import tasks
import asyncio
from database import get_conn, put_conn, add_to_bank, log_transaction
from datetime import datetime, timezone

def start_tasks(bot):
    daily_bank_distribution.start(bot)
    check_expired_auctions.start(bot)
    print("✅ Background tasks started.")

@tasks.loop(hours=24)
async def daily_bank_distribution(bot):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM bank WHERE id = 1")
            bank = cur.fetchone()
            if not bank or bank['balance'] <= 0:
                return

            cur.execute("SELECT COUNT(*) as cnt FROM users")
            result = cur.fetchone()
            user_count = result['cnt'] if result else 0
            if user_count == 0:
                return

            share = round(bank['balance'] / user_count, 4)
            cur.execute("UPDATE bank SET balance = 0, last_distributed = NOW() WHERE id = 1")
            cur.execute("UPDATE users SET cash = cash + %s", (share,))

            cur.execute("SELECT user_id FROM users")
            users = cur.fetchall()
            for u in users:
                log_transaction(u['user_id'], 'bank_dividend', share, f"Daily bank dividend (${share:.4f} per user)")

        conn.commit()
        print(f"✅ Bank distributed ${share:.4f} to {user_count} users.")
    finally:
        put_conn(conn)

@tasks.loop(minutes=1)
async def check_expired_auctions(bot):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM marketplace 
                WHERE status = 'active' AND ends_at <= NOW()
            """)
            expired = cur.fetchall()

            for listing in expired:
                if listing['highest_bidder']:
                    # Transfer coin to winner
                    tax = round(listing['current_bid'] * 0.10, 4)
                    seller_gets = round(listing['current_bid'] - tax, 4)

                    cur.execute("UPDATE coins SET owner_id = %s, for_sale = FALSE WHERE coin_id = %s",
                                (listing['highest_bidder'], listing['coin_id']))
                    cur.execute("UPDATE users SET cash = cash + %s WHERE user_id = %s",
                                (seller_gets, listing['seller_id']))
                    cur.execute("UPDATE bank SET balance = balance + %s WHERE id = 1", (tax,))
                    cur.execute("UPDATE marketplace SET status = 'sold' WHERE listing_id = %s", (listing['listing_id'],))

                    log_transaction(listing['seller_id'], 'auction_sold', seller_gets,
                                    f"Auction sold coin #{listing['coin_id']} (tax: ${tax:.4f})")
                    log_transaction(listing['highest_bidder'], 'auction_won', -listing['current_bid'],
                                    f"Won auction for coin #{listing['coin_id']}")
                else:
                    # No bids — return coin to seller
                    cur.execute("UPDATE coins SET for_sale = FALSE WHERE coin_id = %s", (listing['coin_id'],))
                    cur.execute("UPDATE marketplace SET status = 'expired' WHERE listing_id = %s", (listing['listing_id'],))

        conn.commit()
    finally:
        put_conn(conn)
