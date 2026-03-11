import os
import logging
import aiohttp
from telegram import LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton

import db

logger = logging.getLogger(__name__)

CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN", "")
CRYPTO_API = "https://pay.crypt.bot/api"

# ─── Pricing ────────────────────────────────────────────────────────────────

PLANS = {
    "sub": {
        "title": "⭐ Pro — безлимит 30 дней",
        "description": "Неограниченные расшифровки. В 5× дешевле Otter.ai.",
        "stars": 299,
        "usdt": "3.00",
        "minutes": None,
        "days": 30,
    },
    "m60": {
        "title": "🎯 Минуты +60 (не сгорают)",
        "description": "+60 минут расшифровок без подписки. Хватит на ~2 недели.",
        "stars": 99,
        "usdt": "1.00",
        "minutes": 60,
        "days": None,
    },
    "m300": {
        "title": "🚀 Минуты +300 (не сгорают)",
        "description": "+300 минут расшифровок. Или возьми Pro — разница всего $0.50.",
        "stars": 249,
        "usdt": "2.50",
        "minutes": 300,
        "days": None,
    },
}


# ─── Upgrade keyboard ────────────────────────────────────────────────────────

def build_upgrade_keyboard(has_cryptobot: bool) -> InlineKeyboardMarkup:
    rows = []
    for plan_key, plan in PLANS.items():
        stars_btn = InlineKeyboardButton(
            f"⭐ {plan['stars']} Stars", callback_data=f"stars_{plan_key}"
        )
        if has_cryptobot:
            crypto_btn = InlineKeyboardButton(
                f"💰 {plan['usdt']} USDT", callback_data=f"crypto_{plan_key}"
            )
            rows.append([stars_btn, crypto_btn])
        else:
            rows.append([stars_btn])
    return InlineKeyboardMarkup(rows)


# ─── Telegram Stars ──────────────────────────────────────────────────────────

async def send_stars_invoice(bot, chat_id: int, plan_key: str):
    plan = PLANS[plan_key]
    await bot.send_invoice(
        chat_id=chat_id,
        title=plan["title"],
        description=plan["description"],
        payload=f"stars_{plan_key}",
        currency="XTR",
        prices=[LabeledPrice(plan["title"], plan["stars"])],
    )


async def handle_successful_payment(update, context):
    payment = update.message.successful_payment
    user_id = update.effective_user.id
    payload = payment.invoice_payload  # e.g. "stars_sub", "stars_m60"

    plan_key = payload.replace("stars_", "")
    plan = PLANS.get(plan_key)
    if not plan:
        logger.error(f"Unknown payment payload: {payload}")
        return

    await db.log_payment(
        user_id=user_id,
        charge_id=payment.telegram_payment_charge_id,
        amount=str(plan["stars"]),
        currency="XTR",
        ptype=plan_key,
    )

    if plan["days"]:
        await db.activate_pro(user_id, days=plan["days"])
        await update.message.reply_text(
            f"✅ *Pro активирован* на {plan['days']} дней!\n\n"
            "Расшифровывай без ограничений 🎙",
            parse_mode="Markdown",
        )
    else:
        await db.add_minutes(user_id, plan["minutes"])
        await update.message.reply_text(
            f"✅ *+{plan['minutes']} минут* добавлено!\n\n"
            "Минуты не сгорают — используй когда удобно 🎙",
            parse_mode="Markdown",
        )


# ─── CryptoBot ───────────────────────────────────────────────────────────────

async def create_crypto_invoice(user_id: int, plan_key: str) -> str | None:
    """Create a CryptoBot invoice. Returns pay_url or None on error."""
    if not CRYPTO_BOT_TOKEN:
        return None
    plan = PLANS[plan_key]
    payload = f"crypto_{plan_key}_{user_id}"
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                f"{CRYPTO_API}/createInvoice",
                headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
                json={
                    "asset": "USDT",
                    "amount": plan["usdt"],
                    "description": plan["description"],
                    "payload": payload,
                    "expires_in": 3600,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            )
            data = await resp.json()
        if data.get("ok"):
            result = data["result"]
            invoice_id = result["invoice_id"]
            pay_url = result["pay_url"]
            await db.save_pending_invoice(invoice_id, user_id, f"crypto_{plan_key}")
            return pay_url
        else:
            logger.error(f"CryptoBot createInvoice error: {data}")
            return None
    except Exception as e:
        logger.error(f"CryptoBot request failed: {e}")
        return None


async def check_crypto_invoices(context):
    """JobQueue callback: poll CryptoBot for paid invoices every 5 seconds."""
    if not CRYPTO_BOT_TOKEN:
        return
    pending = await db.get_pending_invoices()
    if not pending:
        return

    invoice_ids = [str(row[0]) for row in pending]
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                f"{CRYPTO_API}/getInvoices",
                headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
                params={"invoice_ids": ",".join(invoice_ids)},
                timeout=aiohttp.ClientTimeout(total=10),
            )
            data = await resp.json()
    except Exception as e:
        logger.error(f"CryptoBot getInvoices failed: {e}")
        return

    if not data.get("ok"):
        return

    for inv in data["result"].get("items", []):
        if inv["status"] != "paid":
            continue
        invoice_id = inv["invoice_id"]
        row = next((r for r in pending if r[0] == invoice_id), None)
        if not row:
            continue
        _, user_id, plan_payload = row  # e.g. "crypto_sub", "crypto_m60"
        plan_key = plan_payload.replace("crypto_", "")
        plan = PLANS.get(plan_key)
        if not plan:
            await db.delete_pending_invoice(invoice_id)
            continue

        await db.log_payment(
            user_id=user_id,
            charge_id=str(invoice_id),
            amount=plan["usdt"],
            currency="USDT",
            ptype=plan_key,
        )
        if plan["days"]:
            await db.activate_pro(user_id, days=plan["days"])
            text = (
                f"✅ *Pro активирован* на {plan['days']} дней!\n\n"
                "Расшифровывай без ограничений 🎙"
            )
        else:
            await db.add_minutes(user_id, plan["minutes"])
            text = (
                f"✅ *+{plan['minutes']} минут* добавлено!\n\n"
                "Минуты не сгорают — используй когда удобно 🎙"
            )

        try:
            await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to notify user {user_id}: {e}")

        await db.delete_pending_invoice(invoice_id)
