import aiosqlite
import os
from datetime import datetime, date, timedelta

DB_PATH = os.getenv("DB_PATH", "bot.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_pro      BOOLEAN  DEFAULT FALSE,
                pro_until   DATETIME,
                credits     INTEGER  DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS daily_usage (
                user_id  INTEGER,
                date     DATE,
                count    INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date)
            );

            CREATE TABLE IF NOT EXISTS payments (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id            INTEGER,
                charge_id          TEXT UNIQUE,
                amount             TEXT,
                currency           TEXT,
                type               TEXT,
                created_at         DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pending_payments (
                invoice_id   INTEGER PRIMARY KEY,
                user_id      INTEGER,
                payload      TEXT,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.commit()


async def get_or_create_user(user_id: int, username: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (user_id, username)
        )
        await db.commit()


FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "10"))


async def check_access(user_id: int) -> str:
    """
    Returns: 'pro' | 'credits' | 'free_ok' | 'limit'
    """
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        # Check pro subscription
        async with db.execute(
            "SELECT is_pro, pro_until FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            is_pro, pro_until = row
            if is_pro and pro_until:
                expiry = datetime.fromisoformat(pro_until)
                if expiry > datetime.utcnow():
                    return "pro"
                else:
                    # expired — reset
                    await db.execute(
                        "UPDATE users SET is_pro = FALSE, pro_until = NULL WHERE user_id = ?",
                        (user_id,)
                    )
                    await db.commit()

        # Check credits
        async with db.execute(
            "SELECT credits FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if row and row[0] > 0:
            return "credits"

        # Check daily free usage
        async with db.execute(
            "SELECT count FROM daily_usage WHERE user_id = ? AND date = ?",
            (user_id, today)
        ) as cur:
            row = await cur.fetchone()
        count = row[0] if row else 0
        if count < FREE_DAILY_LIMIT:
            return "free_ok"
        return "limit"


async def consume_access(user_id: int, access_type: str):
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        if access_type == "credits":
            await db.execute(
                "UPDATE users SET credits = MAX(0, credits - 1) WHERE user_id = ?",
                (user_id,)
            )
        elif access_type == "free_ok":
            await db.execute(
                """INSERT INTO daily_usage (user_id, date, count) VALUES (?, ?, 1)
                   ON CONFLICT(user_id, date) DO UPDATE SET count = count + 1""",
                (user_id, today)
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


async def add_credits(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET credits = credits + ? WHERE user_id = ?",
            (amount, user_id)
        )
        await db.commit()


async def log_payment(user_id: int, charge_id: str, amount: str, currency: str, ptype: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO payments (user_id, charge_id, amount, currency, type) VALUES (?,?,?,?,?)",
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
            return {"is_pro": False, "pro_until": None, "credits": 0, "daily_count": 0}
        is_pro, pro_until, credits = row

        # Check if pro expired
        if is_pro and pro_until:
            expiry = datetime.fromisoformat(pro_until)
            if expiry <= datetime.utcnow():
                is_pro = False
                pro_until = None

        async with db.execute(
            "SELECT count FROM daily_usage WHERE user_id = ? AND date = ?",
            (user_id, today)
        ) as cur:
            usage_row = await cur.fetchone()
        daily_count = usage_row[0] if usage_row else 0

        return {
            "is_pro": is_pro,
            "pro_until": pro_until,
            "credits": credits,
            "daily_count": daily_count,
        }
