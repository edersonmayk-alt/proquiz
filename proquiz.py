import os
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Salom! Quiz bot ishlayapti ✅")

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN topilmadi. Render'da Environment Variables ga BOT_TOKEN qo‘ying.")

    # Render/Python da event loop muammosini oldini olamiz
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))

    app.run_polling()

if __name__ == "__main__":
    main()
