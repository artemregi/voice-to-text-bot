import os
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


def fmt_sec(s: int) -> str:
    """Format seconds as M:SS."""
    return f"{s // 60}:{s % 60:02d}"


# ─── /start ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await db.get_or_create_user(user.id, user.username)

    s = await db.get_user_status(user.id)

    if s["is_pro"]:
        status_line = "⭐ У тебя активна *Pro-подписка* — расшифровки без ограничений!"
    elif s["credits"] > 0:
        mins = s["credits"] // 60
        status_line = f"🎯 У тебя *{mins} мин* накопленных минут — используй когда удобно."
    else:
        used = fmt_sec(s["daily_seconds"])
        count = s["daily_count"]
        status_line = (
            f"🆓 Сегодня использовано *{used} из 3:00* и *{count}/10* расшифровок."
        )

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

    if s["is_pro"] and s["pro_until"]:
        from datetime import datetime
        expiry = datetime.fromisoformat(s["pro_until"])
        days_left = (expiry - datetime.utcnow()).days
        plan_text = f"⭐ *Pro-подписка* — осталось {days_left} дн. (до {expiry.strftime('%d.%m.%Y')})"
    elif s["credits"] > 0:
        mins = s["credits"] // 60
        plan_text = f"🎯 Накопленные минуты: *{mins} мин* (не сгорают)"
    else:
        used = fmt_sec(s["daily_seconds"])
        plan_text = (
            f"🆓 Бесплатный план:\n"
            f"   ⏱ *{used} из 3:00* минут сегодня\n"
            f"   🔢 *{s['daily_count']}/10* расшифровок сегодня"
        )

    await update.message.reply_text(
        "📊 *Твой статус:*\n\n" + plan_text + "\n\nХочешь больше? /upgrade 👇",
        parse_mode="Markdown",
    )


# ─── /upgrade ────────────────────────────────────────────────────────────────

async def upgrade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await db.get_or_create_user(user.id, user.username)

    keyboard = payments.build_upgrade_keyboard(has_cryptobot=bool(CRYPTO_BOT_TOKEN))
    await update.message.reply_text(
        "💎 *Выбери тариф:*\n\n"
        "⭐ *Pro — 30 дней безлимита*\n"
        "   $3 · ~$0.10 за расшифровку · в 5× дешевле Otter.ai\n\n"
        "🎯 *Минуты +60* (не сгорают)\n"
        "   $1 · хватит на ~2 недели обычного использования\n\n"
        "🚀 *Минуты +300* (не сгорают)\n"
        "   $2.50 · или возьми Pro за $3 — разница $0.50, но безлимит\n\n"
        "Выбери способ оплаты 👇",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ─── Transcribe (main handler) ───────────────────────────────────────────────

async def transcribe_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user

    await db.get_or_create_user(user.id, user.username)

    # ── Determine file and duration ──
    if message.voice:
        file_id = message.voice.file_id
        ext = ".ogg"
        duration = message.voice.duration or 0
    elif message.audio:
        file_id = message.audio.file_id
        ext = "." + (message.audio.mime_type.split("/")[-1] if message.audio.mime_type else "mp3")
        duration = message.audio.duration or 0
    elif message.video_note:
        file_id = message.video_note.file_id
        ext = ".mp4"
        duration = message.video_note.duration or 0
    else:
        return

    # ── Access check with duration ──
    access, remaining_sec = await db.check_access(user.id, duration_sec=duration)

    if access == "limit":
        s = await db.get_user_status(user.id)
        keyboard = payments.build_upgrade_keyboard(has_cryptobot=bool(CRYPTO_BOT_TOKEN))
        await message.reply_text(
            f"🔒 Исчерпан лимит на сегодня — *{fmt_sec(s['daily_seconds'])} из 3:00* "
            f"и *{s['daily_count']}/10* расшифровок.\n\n"
            "Pro открывает безлимит за $3/мес 👇",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
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

        # ── Partial: soft wall — show proportional text, hide the rest ──
        if access == "partial":
            fraction = remaining_sec / max(duration, 1)
            words = text.split()
            visible_count = max(20, int(len(words) * fraction))
            visible_text = " ".join(words[:visible_count])
            hidden_count = len(words) - visible_count

            await db.consume_access(user.id, "partial", seconds=remaining_sec)
            logger.info(
                f"Partial transcription: user={user.id}, shown={visible_count}/{len(words)} words, "
                f"remaining={remaining_sec}s, duration={duration}s"
            )

            escaped = html.escape(visible_text)
            await status_msg.edit_text(
                f"📝 Расшифровка (начало):\n\n<code>{escaped}</code>",
                parse_mode="HTML",
            )

            keyboard = payments.build_upgrade_keyboard(has_cryptobot=bool(CRYPTO_BOT_TOKEN))
            await message.reply_text(
                f"🔒 Ещё ~{hidden_count} слов скрыто — исчерпан лимит 3 мин/день.\n\n"
                f"Pro — безлимит за $3/мес 👇",
                reply_markup=keyboard,
            )
            return

        # ── Full transcription (free_ok, pro, credits) ──
        consume_seconds = duration if access != "pro" else 0
        await db.consume_access(user.id, access, seconds=consume_seconds)

        logger.info(f"Transcribed {len(text)} chars, user={user.id}, access={access}, dur={duration}s")

        # Format and send (with chunking for long texts)
        escaped = html.escape(text)
        OPEN, CLOSE = "<code>", "</code>"
        TAG_LEN = len(OPEN) + len(CLOSE)
        header = "📝 Расшифровка:\n\n"

        chunks = []
        remaining_text = escaped
        first = True
        while remaining_text:
            prefix = header if first else ""
            available = 4096 - len(prefix) - TAG_LEN
            chunk = remaining_text[:available]
            chunks.append(prefix + OPEN + chunk + CLOSE)
            remaining_text = remaining_text[available:]
            first = False

        await status_msg.edit_text(chunks[0], parse_mode="HTML")
        for chunk in chunks[1:]:
            await message.reply_text(chunk, parse_mode="HTML")

        # ── Progress footer for free users ──
        if access == "free_ok":
            s = await db.get_user_status(user.id)
            used_now = s["daily_seconds"]
            pct = used_now / db.FREE_DAILY_SECONDS

            if pct >= 0.8:
                left = db.FREE_DAILY_SECONDS - used_now
                await message.reply_text(
                    f"⚠️ Осталось ~{fmt_sec(left)} бесплатного времени сегодня.\n"
                    f"Pro — безлимит за $3/мес → /upgrade"
                )
            elif pct >= 0.4:
                await message.reply_text(
                    f"▸ {fmt_sec(used_now)} из 3:00 использовано сегодня"
                )

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

    parts = query.data.split("_", 1)
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
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("💰 Оплатить в @CryptoBot", url=pay_url)]]
            )
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
    await update.pre_checkout_query.answer(ok=True)


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан в .env файле")
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY не задан в .env файле")

    import asyncio
    asyncio.get_event_loop().run_until_complete(db.init_db())

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("upgrade", upgrade_cmd))

    app.add_handler(MessageHandler(filters.VOICE, transcribe_voice))
    app.add_handler(MessageHandler(filters.AUDIO, transcribe_voice))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, transcribe_voice))

    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r"^(stars|crypto)_(sub|m60|m300)$"))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, payments.handle_successful_payment))

    if CRYPTO_BOT_TOKEN:
        app.job_queue.run_repeating(payments.check_crypto_invoices, interval=5, first=5)
        logger.info("CryptoBot polling enabled")

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
