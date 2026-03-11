import aiosqlite
import os
from datetime import datetime, date, timedelta

DB_PATH = os.getenv("DB_PATH", "bot.db")

FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "10"))
FREE_DAILY_SECONDS = int(os.getenv("FREE_DAILY_SECONDS", "180"))


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id              INTEGER PRIMARY KEY,
                username             TEXT,
                created_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_pro               BOOLEAN  DEFAULT FALSE,
                pro_until            DATETIME,
                credits              INTEGER  DEFAULT 0,
                referred_by          INTEGER  DEFAULT NULL,
                partner_earned_min   INTEGER  DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS daily_usage (
                user_id  INTEGER,
                date     DATE,
                count    INTEGER DEFAULT 0,
                seconds  INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date)
            );

            CREATE TABLE IF NOT EXISTS payments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                charge_id  TEXT UNIQUE,
                amount     TEXT,
                currency   TEXT,
                type       TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pending_payments (
                invoice_id INTEGER PRIMARY KEY,
                user_id    INTEGER,
                payload    TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Migrations for existing DBs
        for migration in [
            "ALTER TABLE daily_usage ADD COLUMN seconds INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN partner_earned_min INTEGER DEFAULT 0",
        ]:
            try:
                await db.execute(migration)
                await db.commit()
            except Exception:
                pass  # Column already exists
        await db.commit()


async def get_or_create_user(user_id: int, username: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (user_id, username)
        )
        await db.commit()


async def set_referral(user_id: int, referrer_id: int):
    """Set who referred this user. Only set once (first touch)."""
    if user_id == referrer_id:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET referred_by = ? WHERE user_id = ? AND referred_by IS NULL",
            (referrer_id, user_id)
        )
        await db.commit()


async def get_referrer(user_id: int) -> int | None:
    """Return referrer's user_id, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT referred_by FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row and row[0] else None


async def add_partner_bonus(referrer_id: int, bonus_minutes: int):
    """Credit bonus minutes to partner and record total earned."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE users
               SET credits = credits + ?,
                   partner_earned_min = partner_earned_min + ?
               WHERE user_id = ?""",
            (bonus_minutes * 60, bonus_minutes, referrer_id)
        )
        await db.commit()


async def get_partner_stats(user_id: int) -> dict:
    """Return how many users this person referred and total minutes earned as partner."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)
        ) as cur:
            ref_count = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT partner_earned_min FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        earned = row[0] if row else 0
    return {"referrals": ref_count, "earned_minutes": earned}


async def check_access(user_id: int, duration_sec: int = 0) -> tuple:
    """
    Returns (access_type, remaining_free_seconds):
      ("pro", 0)      — unlimited pro subscription
      ("credits", 0)  — has paid minute credits, transcribe fully
      ("free_ok", N)  — within free limits, N free seconds remain today
      ("partial", N)  — N free seconds remain but duration > N (soft wall)
      ("limit", 0)    — both free limits exhausted
    """
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        # Pro check
        async with db.execute(
            "SELECT is_pro, pro_until FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            is_pro, pro_until = row
            if is_pro and pro_until:
                if datetime.fromisoformat(pro_until) > datetime.utcnow():
                    return ("pro", 0)
                else:
                    await db.execute(
                        "UPDATE users SET is_pro = FALSE, pro_until = NULL WHERE user_id = ?",
                        (user_id,)
                    )
                    await db.commit()

        # Credits check (stored in seconds)
        async with db.execute(
            "SELECT credits FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if row and row[0] > 0:
            return ("credits", 0)

        # Daily usage
        async with db.execute(
            "SELECT count, seconds FROM daily_usage WHERE user_id = ? AND date = ?",
            (user_id, today)
        ) as cur:
            row = await cur.fetchone()
        used_count = row[0] if row else 0
        used_sec = row[1] if row else 0

        # Count limit
        if used_count >= FREE_DAILY_LIMIT:
            return ("limit", 0)

        # Time limit fully exhausted
        if used_sec >= FREE_DAILY_SECONDS:
            return ("limit", 0)

        remaining_sec = FREE_DAILY_SECONDS - used_sec

        # Fits within remaining free time
        if duration_sec == 0 or used_sec + duration_sec <= FREE_DAILY_SECONDS:
            return ("free_ok", remaining_sec)

        # Partially fits — soft wall
        return ("partial", remaining_sec)


async def consume_access(user_id: int, access_type: str, seconds: int = 0):
    """
    Deduct usage based on access type.
    - "credits": subtract seconds from users.credits (floored at 0)
    - "free_ok" / "partial": increment daily count and seconds
    - "pro": no-op
    """
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        if access_type == "credits":
            await db.execute(
                "UPDATE users SET credits = MAX(0, credits - ?) WHERE user_id = ?",
                (seconds, user_id)
            )
        elif access_type in ("free_ok", "partial"):
            await db.execute(
                """INSERT INTO daily_usage (user_id, date, count, seconds) VALUES (?, ?, 1, ?)
                   ON CONFLICT(user_id, date) DO UPDATE SET
                       count = count + 1,
                       seconds = seconds + ?""",
                (user_id, today, seconds, seconds)
            )
        await db.commit()


async def activate_pro(user_id: int, days: int = 30):
    pro_until = (datetime.utcnow() + timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_pro = TRUE, pro_until = ? WHERE user_id = ?",
            (pro_until, user_id)
        )
        await db.commit()


async def add_minutes(user_id: int, minutes: int):
    """Add purchased minutes (converted to seconds) to the user's credits balance."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET credits = credits + ? WHERE user_id = ?",
            (minutes * 60, user_id)
        )
        await db.commit()


async def log_payment(user_id: int, charge_id: str, amount: str, currency: str, ptype: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO payments (user_id, charge_id, amount, currency, type) "
            "VALUES (?,?,?,?,?)",
            (user_id, charge_id, amount, currency, ptype)
        )
        await db.commit()


async def save_pending_invoice(invoice_id: int, user_id: int, payload: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO pending_payments (invoice_id, user_id, payload) VALUES (?,?,?)",
            (invoice_id, user_id, payload)
        )
        await db.commit()


async def get_pending_invoices() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT invoice_id, user_id, payload FROM pending_payments"
        ) as cur:
            return await cur.fetchall()


async def delete_pending_invoice(invoice_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pending_payments WHERE invoice_id = ?", (invoice_id,))
        await db.commit()


async def get_user_status(user_id: int) -> dict:
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_pro, pro_until, credits FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return {
                "is_pro": False, "pro_until": None,
                "credits": 0, "daily_count": 0, "daily_seconds": 0
            }
        is_pro, pro_until, credits = row

        if is_pro and pro_until:
            if datetime.fromisoformat(pro_until) <= datetime.utcnow():
                is_pro = False
                pro_until = None

        async with db.execute(
            "SELECT count, seconds FROM daily_usage WHERE user_id = ? AND date = ?",
            (user_id, today)
        ) as cur:
            usage_row = await cur.fetchone()

        return {
            "is_pro": is_pro,
            "pro_until": pro_until,
            "credits": credits,           # stored in seconds
            "daily_count": usage_row[0] if usage_row else 0,
            "daily_seconds": usage_row[1] if usage_row else 0,
        }
