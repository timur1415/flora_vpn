import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, Application

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="I'm a bot, please talk to me!")

def init_bot() -> Application:
    application = ApplicationBuilder().token('8403456802:AAHqFIEtY0OmLbqqt5KTdOmco_zS3AvwnEI').build()
    
    start_handler = CommandHandler('start', start)
    application.add_handler(start_handler)
    
    return application
