"""
Telegram bot — persistent bidirectional channel with the athlete.

Runs 24/7 on Railway. Handles incoming messages from the athlete and responds
as the coach. All conversations are logged to Coach Memory (Telegram Log tab).

Commands:
  /start   — greeting from the coach
  /summary — quick weekly progress snapshot

Any other text is treated as a question to the coach.
Routing: short/simple questions → Haiku (fast), longer/complex → Sonnet.
"""

import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import anthropic

# Import project modules (bot runs from repo root or src/)
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from config import (
    ANTHROPIC_API_KEY, ATHLETE_NAME, CLAUDE_MODEL,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    bootstrap_google_credentials,
)

# Write Google credential files from env vars if running on Railway/CI
bootstrap_google_credentials()

from prompt import SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Model routing
# ---------------------------------------------------------------------------

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = CLAUDE_MODEL  # claude-sonnet-4-6

# Use Haiku for short conversational messages, Sonnet for complex queries
HAIKU_THRESHOLD_WORDS = 20  # if message < 20 words, use Haiku


def _choose_model(message_text: str) -> str:
    word_count = len(message_text.split())
    return HAIKU_MODEL if word_count < HAIKU_THRESHOLD_WORDS else SONNET_MODEL


# ---------------------------------------------------------------------------
# Build context for the bot response
# ---------------------------------------------------------------------------

def _build_bot_context() -> str:
    """
    Build a rich context string for the bot: recent Telegram history + brief memory summary.
    Kept lightweight — the bot needs to respond fast.
    """
    try:
        from memory import (
            read_telegram_log, read_athlete_profile, read_long_term_goals,
            read_lift_history, read_strategic_plan, read_planning_notes,
            get_last_run_date,
        )

        sections = []

        # Recent Telegram conversation (last 10 messages for context)
        tg_log = read_telegram_log(limit=10)
        if tg_log:
            lines = []
            for entry in tg_log:
                direction = entry.get("Direction", "")
                msg = entry.get("Message", "").strip()
                d = entry.get("Date", "")
                label = ATHLETE_NAME if direction == "IN" else "Coach"
                lines.append(f"  [{d}] {label}: {msg}")
            sections.append("RECENT CONVERSATION\n" + "\n".join(lines))

        # Athlete profile (brief)
        profile = read_athlete_profile()
        if profile:
            # Only first 3 lines to keep it compact
            profile_brief = "\n".join(profile.split("\n")[:3])
            sections.append(f"ATHLETE\n{profile_brief}")

        # Long-term goals
        goals = read_long_term_goals()
        if goals:
            goals_brief = "\n".join(goals.split("\n")[:4])
            sections.append(f"GOALS\n{goals_brief}")

        # Current strategic plan (if available)
        plan = read_strategic_plan()
        active_phases = [p for p in plan if not p.get("Phase", "").startswith("#")]
        if active_phases:
            from datetime import date
            today = date.today()
            for p in active_phases:
                try:
                    from datetime import datetime as _dt
                    s = _dt.strptime(p.get("Start Date", ""), "%Y-%m-%d").date()
                    e = _dt.strptime(p.get("End Date", ""), "%Y-%m-%d").date()
                    if s <= today <= e:
                        sections.append(
                            f"CURRENT TRAINING PHASE\n"
                            f"  {p.get('Phase', '?')} ({p.get('Start Date', '?')} → {p.get('End Date', '?')})\n"
                            f"  Focus: {p.get('Focus', '?')}\n"
                            f"  Targets: {p.get('Key Targets', '?')}"
                        )
                        break
                except (ValueError, TypeError):
                    pass

        # Recent 1RM snapshot (last reading per key lift)
        lift_history = read_lift_history(limit=50)
        if lift_history:
            key_lifts = ["Squat", "Bench Press", "Deadlift", "OHP"]
            lift_lines = []
            for lift in key_lifts:
                for row in reversed(lift_history):
                    if lift.lower() in row.get("Exercise", "").lower():
                        est = row.get("Est 1RM", "")
                        if est:
                            lift_lines.append(f"  {lift}: {est}kg est. 1RM [{row.get('Date', '?')}]")
                            break
            if lift_lines:
                sections.append("CURRENT LIFT LEVELS\n" + "\n".join(lift_lines))

        context = "\n\n---\n\n".join(sections)
        return context

    except Exception as e:
        return f"[Context unavailable: {e}]"


# ---------------------------------------------------------------------------
# Claude response generation
# ---------------------------------------------------------------------------

def _generate_response(user_message: str, context: str, model: str) -> str:
    """Generate a coaching response via Claude."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    bot_system = SYSTEM_PROMPT + (
        "\n\nYou are responding via Telegram — keep replies concise and conversational. "
        "This is a quick check-in, not a full coaching email. 1-4 sentences unless the question genuinely needs more. "
        "No section headers. Natural tone."
    )

    full_message = f"{context}\n\n---\n\nATHLETE MESSAGE (via Telegram): {user_message}\n\nReply as the coach."

    message = client.messages.create(
        model=model,
        max_tokens=400,
        system=bot_system,
        messages=[{"role": "user", "content": full_message}]
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Log to Coach Memory
# ---------------------------------------------------------------------------

def _log_message(direction: str, text: str) -> None:
    """Log a Telegram message to Coach Memory (best-effort, non-fatal)."""
    try:
        from memory import append_telegram_log
        append_telegram_log(direction=direction, message=text)
    except Exception as e:
        print(f"[Telegram] Log failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Guard: only respond to the athlete's chat
# ---------------------------------------------------------------------------

def _is_authorized(update: Update) -> bool:
    if not TELEGRAM_CHAT_ID:
        return True  # No restriction set — allow all (dev mode)
    return str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_start(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return

    greeting = (
        f"Hey {ATHLETE_NAME}. I'm here whenever you need me — ask about training, "
        "progress, how you're tracking, anything. What's on your mind?"
    )
    await update.message.reply_text(greeting)
    _log_message("OUT", greeting)


async def handle_summary(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return

    await update.message.reply_text("Pulling your summary...")

    ctx = _build_bot_context()
    response = _generate_response(
        "Give me a quick summary of how training is going this week — key numbers, momentum, anything I should know.",
        ctx,
        HAIKU_MODEL,
    )
    await update.message.reply_text(response)
    _log_message("IN", "/summary")
    _log_message("OUT", response)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await update.message.reply_text("Sorry, I only talk to my athlete.")
        return

    user_text = update.message.text.strip()
    if not user_text:
        return

    _log_message("IN", user_text)

    # Show typing indicator while generating
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action="typing",
    )

    model = _choose_model(user_text)
    ctx = _build_bot_context()
    response = _generate_response(user_text, ctx, model)

    await update.message.reply_text(response)
    _log_message("OUT", response)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set. Cannot start bot.", file=sys.stderr)
        sys.exit(1)

    print(f"Starting Telegram bot for {ATHLETE_NAME}...")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("summary", handle_summary))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot running. Waiting for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
