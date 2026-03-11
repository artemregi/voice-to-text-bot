import os
import html
import logging
import tempfile
from dotenv import load_dotenv
from groq import Groq
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет!\n\n"
        "Отправь мне голосовое сообщение или аудиофайл — я расшифрую его в текст.\n\n"
        "🎙 Поддерживаю русский и английский язык.\n"
        "📎 Работаю с: голосовыми, аудио и видео-кружками."
    )


async def transcribe_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message

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

        logger.info(f"Transcribed {len(text)} chars from user {message.from_user.id}")

        # Escape HTML, wrap in <code> — gives copy-on-tap button in Telegram
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
            "• GROQ_API_KEY правильный\n"
            "• Попробуй ещё раз"
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан в .env файле")
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY не задан в .env файле")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE, transcribe_voice))
    app.add_handler(MessageHandler(filters.AUDIO, transcribe_voice))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, transcribe_voice))

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
