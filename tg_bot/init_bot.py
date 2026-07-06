import logging
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, Application

from config.config import BOT_TOKEN, WEBHOOK_URL

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mini_app_url = (WEBHOOK_URL or "").rstrip("/") + "/"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="Open floraVPN",
                    web_app=WebAppInfo(url=mini_app_url),
                )
            ]
        ]
    )

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Open your Flora VPN mini app:",
        reply_markup=keyboard,
    )


def init_bot() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    start_handler = CommandHandler("start", start)
    application.add_handler(start_handler)

    return application
