import os
import re
import html
import logging
import tempfile
from dotenv import load_dotenv
from groq import Groq
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    filters,
    ContextTypes,
)

import db
import payments

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN", "")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY)


# ─── /start ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await db.get_or_create_user(user.id, user.username)

    status = await db.get_user_status(user.id)
    limit = int(os.getenv("FREE_DAILY_LIMIT", "10"))

    if status["is_pro"]:
        status_line = "⭐ У тебя активна *Pro-подписка* — расшифровки без ограничений!"
    elif status["credits"] > 0:
        status_line = f"🎯 У тебя *{status['credits']} кредитов* — хватит на {status['credits']} расшифровок."
    else:
        used = status["daily_count"]
        status_line = f"🆓 Сегодня использовано *{used}/{limit}* бесплатных расшифровок."

    await update.message.reply_text(
        "👋 Привет!\n\n"
        "Отправь мне голосовое сообщение или аудиофайл — я расшифрую его в текст.\n\n"
        "🎙 Поддерживаю русский и английский язык.\n"
        "📎 Работаю с: голосовыми, аудио и видео-кружками.\n\n"
        + status_line,
        parse_mode="Markdown",
    )


# ─── /status ─────────────────────────────────────────────────────────────────

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await db.get_or_create_user(user.id, user.username)

    s = await db.get_user_status(user.id)
    limit = int(os.getenv("FREE_DAILY_LIMIT", "10"))

    if s["is_pro"] and s["pro_until"]:
        from datetime import datetime
        expiry = datetime.fromisoformat(s["pro_until"])
        days_left = (expiry - datetime.utcnow()).days
        plan_text = f"⭐ *Pro-подписка* — осталось {days_left} дн. (до {expiry.strftime('%d.%m.%Y')})"
    elif s["credits"] > 0:
        plan_text = f"🎯 Кредиты: *{s['credits']}* расшифровок"
    else:
        plan_text = f"🆓 Бесплатный план: *{s['daily_count']}/{limit}* сегодня"

    text = (
        "📊 *Твой статус:*\n\n"
        + plan_text + "\n\n"
        "Хочешь больше? Нажми /upgrade 👇"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ─── /upgrade ────────────────────────────────────────────────────────────────

async def upgrade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await db.get_or_create_user(user.id, user.username)

    keyboard = payments.build_upgrade_keyboard(has_cryptobot=bool(CRYPTO_BOT_TOKEN))
    await update.message.reply_text(
        "💎 *Выбери тариф:*\n\n"
        "⭐ *Pro — 30 дней* — безлимитные расшифровки\n"
        "🎯 *Кредиты +30* — 30 разовых расшифровок\n"
        "🚀 *Кредиты +150* — 150 разовых расшифровок\n\n"
        "Выбери способ оплаты 👇",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ─── Transcribe (main handler) ───────────────────────────────────────────────

async def transcribe_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user

    await db.get_or_create_user(user.id, user.username)

    # ── Access check ──
    access = await db.check_access(user.id)
    if access == "limit":
        keyboard = payments.build_upgrade_keyboard(has_cryptobot=bool(CRYPTO_BOT_TOKEN))
        limit = int(os.getenv("FREE_DAILY_LIMIT", "10"))
        await message.reply_text(
            f"🔒 Использовано *{limit}/{limit}* расшифровок сегодня.\n\n"
            "Выбери тариф для продолжения 👇",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return

    # ── Determine file ──
    if message.voice:
        file_id = message.voice.file_id
        ext = ".ogg"
    elif message.audio:
        file_id = message.audio.file_id
        ext = "." + (message.audio.mime_type.split("/")[-1] if message.audio.mime_type else "mp3")
    elif message.video_note:
        file_id = message.video_note.file_id
        ext = ".mp4"
    else:
        return

    status_msg = await message.reply_text("⏳ Расшифровываю...")

    tmp_path = None
    try:
        tg_file = await context.bot.get_file(file_id)

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp_path = tmp.name

        await tg_file.download_to_drive(tmp_path)

        with open(tmp_path, "rb") as audio_file:
            transcription = groq_client.audio.transcriptions.create(
                model="whisper-large-v3-turbo",
                file=audio_file,
                response_format="text"
            )

        text = transcription.strip() if isinstance(transcription, str) else transcription.text.strip()

        if not text:
            await status_msg.edit_text("🤷 Не удалось распознать речь. Попробуй ещё раз.")
            return

        # ── Consume access after successful transcription ──
        await db.consume_access(user.id, access)

        logger.info(f"Transcribed {len(text)} chars from user {user.id} (access={access})")

        # ── Format and send ──
        escaped = html.escape(text)
        OPEN, CLOSE = "<code>", "</code>"
        TAG_LEN = len(OPEN) + len(CLOSE)
        header = "📝 Расшифровка:\n\n"

        chunks = []
        remaining = escaped
        first = True
        while remaining:
            prefix = header if first else ""
            available = 4096 - len(prefix) - TAG_LEN
            chunk = remaining[:available]
            chunks.append(prefix + OPEN + chunk + CLOSE)
            remaining = remaining[available:]
            first = False

        await status_msg.edit_text(chunks[0], parse_mode="HTML")
        for chunk in chunks[1:]:
            await message.reply_text(chunk, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Transcription error: {e}")
        await status_msg.edit_text(
            "❌ Ошибка при расшифровке. Проверь:\n"
            "• Файл не слишком большой (до 25 МБ)\n"
            "• Попробуй ещё раз"
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


# ─── Buy callbacks ────────────────────────────────────────────────────────────

async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data  # e.g. "stars_sub", "crypto_c30"
    parts = data.split("_", 1)
    if len(parts) != 2:
        return
    method, plan_key = parts

    if plan_key not in payments.PLANS:
        await query.answer("Неизвестный тариф.", show_alert=True)
        return

    if method == "stars":
        await payments.send_stars_invoice(context.bot, query.message.chat_id, plan_key)

    elif method == "crypto":
        if not CRYPTO_BOT_TOKEN:
            await query.answer("Крипто-оплата временно недоступна.", show_alert=True)
            return
        pay_url = await payments.create_crypto_invoice(query.from_user.id, plan_key)
        if pay_url:
            plan = payments.PLANS[plan_key]
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("💰 Оплатить в @CryptoBot", url=pay_url)]])
            await query.message.reply_text(
                f"💳 *{plan['title']}* — {plan['usdt']} USDT\n\n"
                "Нажми кнопку ниже для оплаты. После оплаты доступ откроется автоматически ✅",
                parse_mode="Markdown",
                reply_markup=kb,
            )
        else:
            await query.answer("Не удалось создать инвойс. Попробуй позже.", show_alert=True)


# ─── Telegram Stars handlers ──────────────────────────────────────────────────

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    await query.answer(ok=True)


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан в .env файле")
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY не задан в .env файле")

    import asyncio
    asyncio.get_event_loop().run_until_complete(db.init_db())

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("upgrade", upgrade_cmd))

    # Media
    app.add_handler(MessageHandler(filters.VOICE, transcribe_voice))
    app.add_handler(MessageHandler(filters.AUDIO, transcribe_voice))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, transcribe_voice))

    # Payments
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r"^(stars|crypto)_(sub|c30|c150)$"))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, payments.handle_successful_payment))

    # CryptoBot polling job (every 5 seconds)
    if CRYPTO_BOT_TOKEN:
        app.job_queue.run_repeating(payments.check_crypto_invoices, interval=5, first=5)
        logger.info("CryptoBot polling enabled")

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
