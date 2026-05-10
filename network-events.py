# network-events.py
# Claude-powered Telegram bot for Glasgow networking events
# Conversational + daily digest at 7:30 AM (Europe/London)
#
# Setup:
#   1. Create a bot via @BotFather on Telegram → copy token
#   2. pip install python-telegram-bot anthropic schedule
#   3. Run: python network-events.py --get-chat-id
#      Then send your bot any message — your chat ID will print.
#   4. Fill in credentials below (or use env vars), then:
#      python network-events.py

import os
import asyncio
import logging
import threading
import schedule
import time
from datetime import datetime
import anthropic
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import sys

# ── CONFIG ────────────────────────────────────────────────────────────────────
def load_env_file(path: str = ".env") -> None:
    """Load simple KEY=VALUE pairs from .env when they are not already exported."""
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


load_env_file()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")
DIGEST_TIME        = "07:30"   # 24h, Europe/London — adjust for UTC offset if hosting abroad
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a helpful networking events assistant. Your job is to:

1. Find and summarise professional networking events happening in Glasgow.
2. Focus on: tech, startups, AI/ML, fintech, product, design, founder, social, and general professional networking events.
3. When searching, always include the current date context to find upcoming events.
4. Format event listings clearly with: name, date/time, venue, price, and a 1-2 sentence description.
5. Be conversational and friendly. If someone asks a follow-up, remember what you discussed.
6. If asked about a specific type (e.g. "fintech only" or "free events"), filter accordingly.
7. You MUST include the link of each event you find in the message.
8. For the daily digest, find 5-7 events happening in the next 7 days.

Keep in mind that the location might be changed according to the user's request.
Keep responses concise for Telegram — list the bullet points to aid scannability.
Use Telegram markdown: *bold*, _italic_, `code`, [text](url)."""

# Per-chat conversation history (in-memory; resets on restart)
histories: dict[str, list] = {}


# ── CLAUDE API ────────────────────────────────────────────────────────────────
def ask_claude(messages: list, extra_note: str = "") -> str:
    system = f"{SYSTEM_PROMPT}\n\n{extra_note}" if extra_note else SYSTEM_PROMPT
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=system,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=messages,
    )
    return "".join(
        block.text for block in response.content if block.type == "text"
    ) or "(No response generated)"


# ── SEND LONG MESSAGES (Telegram 4096 char limit) ────────────────────────────
async def send_message(bot, chat_id: str, text: str):
    limit = 4000
    if len(text) <= limit:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        return
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current += ("\n" if current else "") + line
    if current:
        chunks.append(current)
    for chunk in chunks:
        await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="Markdown")
        await asyncio.sleep(0.3)


# ── DAILY DIGEST ─────────────────────────────────────────────────────────────
async def send_daily_digest(bot, chat_id: str):
    today = datetime.now().strftime("%A %-d %B %Y")
    log.info(f"Sending daily digest to {chat_id}")
    await bot.send_message(
        chat_id=chat_id,
        text="🔍 _Finding this week's networking events..._",
        parse_mode="Markdown",
    )
    messages = [
        {
            "role": "user",
            "content": (
                f"Today is {today}. Please search for and list the best professional "
                "networking events happening in Glasgow over the next 7 days. "
                "Give me the daily digest format with 5-7 events."
            ),
        }
    ]
    try:
        reply = ask_claude(messages, extra_note="This is an automated daily digest request.")
        header = f"*🗓️ Glasgow Networking Digest*\n_{today}_\n\n"
        await send_message(bot, chat_id, header + reply)
        histories[chat_id] = messages + [{"role": "assistant", "content": reply}]
    except Exception as e:
        log.error(f"Digest error: {e}")
        await bot.send_message(chat_id=chat_id, text="⚠️ Could not fetch events. Try /events to retry.")


# ── CONVERSATION HANDLER ──────────────────────────────────────────────────────
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = str(update.effective_chat.id)
    if chat_id not in histories:
        histories[chat_id] = []

    histories[chat_id].append({"role": "user", "content": text})
    # Keep last 20 messages to avoid token bloat
    if len(histories[chat_id]) > 20:
        histories[chat_id] = histories[chat_id][-20:]

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(None, ask_claude, histories[chat_id])
        histories[chat_id].append({"role": "assistant", "content": reply})
        await send_message(context.bot, chat_id, reply)
    except Exception as e:
        log.error(f"Message handler error: {e}")
        histories[chat_id].pop()  # remove failed user message
        await update.message.reply_text("⚠️ Something went wrong. Please try again.")


# ── COMMANDS ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    histories[chat_id] = []
    await update.message.reply_text(
        "👋 *Hey! I'm your Glasgow networking events bot.*\n\n"
        "I'll send you a daily digest at 07:30 and you can ask me anything:\n\n"
        "• /events — today's roundup\n"
        "• /free — free events only\n"
        "• /fintech — fintech events\n"
        "• /thisweek — full week view\n"
        "• /clear — reset conversation\n\n"
        'Or just chat: _"Any AI meetups this Friday?"_',
        parse_mode="Markdown",
    )

async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_daily_digest(context.bot, str(update.effective_chat.id))

async def cmd_thisweek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_user_message(update, context,
        "Show me all networking events in Glasgow this week, grouped by day.")

async def cmd_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_user_message(update, context,
        "Find free networking events in Glasgow this week.")

async def cmd_fintech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_user_message(update, context,
        "Find fintech and finance-related networking events in Glasgow this week.")

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    histories[str(update.effective_chat.id)] = []
    await update.message.reply_text("🧹 Conversation cleared. Fresh start!")

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await handle_user_message(update, context, update.message.text)


# ── SCHEDULER (runs in a background thread) ───────────────────────────────────
def run_scheduler(app):
    def trigger_digest():
        log.info("Cron triggered — sending daily digest")
        asyncio.run(send_daily_digest(app.bot, TELEGRAM_CHAT_ID))

    schedule.every().day.at(DIGEST_TIME).do(trigger_digest)
    log.info(f"Daily digest scheduled at {DIGEST_TIME} (local time)")
    while True:
        schedule.run_pending()
        time.sleep(30)


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    # Helper mode: print chat ID then exit
    if "--get-chat-id" in sys.argv:
        print("Send any message to your bot in Telegram, then check below...")
        async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
            print(f"\n✅ Your Chat ID is: {update.effective_chat.id}\n")
            print("Add it to TELEGRAM_CHAT_ID, then restart without --get-chat-id")
            await update.message.reply_text(f"Your chat ID is: `{update.effective_chat.id}`", parse_mode="Markdown")
            sys.exit(0)
        app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        app.add_handler(MessageHandler(filters.ALL, get_id))
        app.run_polling()
        return

    # Digest-only mode (for external cron jobs)
    if "--digest-only" in sys.argv:
        async def run():
            from telegram import Bot
            bot = Bot(token=TELEGRAM_BOT_TOKEN)
            await send_daily_digest(bot, TELEGRAM_CHAT_ID)
        asyncio.run(run())
        return

    # Normal mode
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("events",   cmd_events))
    app.add_handler(CommandHandler("thisweek", cmd_thisweek))
    app.add_handler(CommandHandler("free",     cmd_free))
    app.add_handler(CommandHandler("fintech",  cmd_fintech))
    app.add_handler(CommandHandler("clear",    cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # Start scheduler in background thread
    scheduler_thread = threading.Thread(target=run_scheduler, args=(app,), daemon=True)
    scheduler_thread.start()

    log.info(f"✅ Bot running. Digest at {DIGEST_TIME}. Send /start in Telegram.")
    app.run_polling()


if __name__ == "__main__":
    main()
