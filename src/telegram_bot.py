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
    Build a rich context string for the bot — same Tier-1 brain as the daily email coach.
    Reads Coach State, Coach Focus, Athlete Preferences, Lift History, and recent Telegram log.
    """
    try:
        from memory import (
            read_telegram_log, read_athlete_profile, read_long_term_goals,
            read_lift_history, read_coach_state, read_coach_focus,
            read_athlete_preferences, read_commands, read_tracked_lifts,
        )

        sections = []

        # --- Tier 1: Coach State (compressed domain summaries — the coach's brain) ---
        coach_state = read_coach_state()
        if coach_state:
            lines = []
            for domain, data in coach_state.items():
                summary = data.get("summary", "").strip()
                if summary:
                    lines.append(f"  [{domain}] {summary}")
            if lines:
                sections.append("COACH STATE (current understanding per domain)\n" + "\n".join(lines))

        # --- Tier 1: Coach Focus (open watch items) ---
        focus_items = read_coach_focus(status_filter="OPEN")
        if focus_items:
            lines = []
            for item in focus_items[:12]:
                tag = item.get("Category", "")
                note = item.get("Item", "").strip()
                priority = item.get("Priority", "")
                badge = f"[{priority}] " if priority in ("HIGH", "PINNED") else ""
                lines.append(f"  {badge}[{tag}] {note}")
            sections.append("COACH FOCUS (open watch items)\n" + "\n".join(lines))

        # --- Tier 1: Athlete Preferences ---
        prefs = read_athlete_preferences()
        if prefs:
            lines = []
            for p in prefs:
                cat = p.get("Category", "")
                pref = p.get("Preference", "").strip()
                lines.append(f"  [{cat}] {pref}")
            sections.append("ATHLETE PREFERENCES\n" + "\n".join(lines))

        # --- Pending proposals (so bot knows what's awaiting confirmation) ---
        commands = read_commands()
        pending = [c for c in commands
                   if c.get("Command", "").upper() == "PENDING_PROPOSAL"
                   and c.get("Applied", "").upper() not in ("Y", "DECLINED")]
        if pending:
            lines = [f"  {p.get('Value', '')[:120]}" for p in pending]
            sections.append("AWAITING ATHLETE CONFIRMATION\n" + "\n".join(lines))

        # --- Athlete profile + goals ---
        profile = read_athlete_profile()
        if profile:
            sections.append(f"ATHLETE PROFILE\n{profile.strip()}")

        goals = read_long_term_goals()
        if goals:
            sections.append(f"LONG-TERM GOALS\n{goals.strip()}")

        # --- Lift levels (best 1RM per tracked lift, word-boundary match) ---
        import re as _re
        tracked_lifts = read_tracked_lifts()
        lift_history = read_lift_history(limit=60)
        if lift_history:
            lift_lines = []
            for tl in tracked_lifts:
                lift = tl["match_pattern"]
                pattern = _re.compile(r"(?i)^" + _re.escape(lift) + r"(\s|$|\()")
                best_est = None
                for row in lift_history:
                    if not pattern.match(row.get("Exercise", "").strip()):
                        continue
                    est = row.get("Est 1RM", "")
                    if est:
                        try:
                            v = float(est)
                            if best_est is None or v > best_est[0]:
                                best_est = (v, row.get("Date", "?"))
                        except ValueError:
                            pass
                if best_est:
                    lift_lines.append(f"  {lift}: {best_est[0]}kg est. 1RM [{best_est[1]}]")
            if lift_lines:
                sections.append("CURRENT LIFT LEVELS\n" + "\n".join(lift_lines))

        # --- Recent Telegram conversation (last 12 messages) ---
        tg_log = read_telegram_log(limit=12)
        if tg_log:
            lines = []
            for entry in tg_log:
                direction = entry.get("Direction", "")
                msg = entry.get("Message", "").strip()
                d = entry.get("Date", "")
                label = ATHLETE_NAME if direction == "IN" else "Coach"
                lines.append(f"  [{d}] {label}: {msg}")
            sections.append("RECENT TELEGRAM CONVERSATION\n" + "\n".join(lines))

        return "\n\n---\n\n".join(sections)

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
        "No section headers. Natural tone.\n\n"
        "CRITICAL: Only state facts that are explicitly present in the context above. "
        "If specific data is missing (exercise names, weights, dates, numbers), say so directly: "
        "'I don't have that detail in front of me right now.' "
        "Never invent training data, exercise names, weights, or results. "
        "Never claim to have access to data that isn't shown in the context."
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
# Confirmation flow for PENDING_PROPOSALs
# ---------------------------------------------------------------------------

_CONFIRM_WORDS = {"yes", "yep", "yeah", "confirm", "confirmed", "do it", "go ahead",
                  "sí", "si", "dale", "ok", "okay", "sure"}
_DECLINE_WORDS = {"no", "nope", "cancel", "reject", "rejected", "don't", "dont",
                  "stop", "forget it", "never mind", "nevermind"}

# Phrases that end an active SKIP_UNTIL (resume emails)
_RESUME_PHRASES = ["resume emails", "end skip", "cancel skip", "unpause", "resume coaching",
                   "reanudar emails", "fin de pausa"]


def _end_skip_until() -> bool:
    """
    Mark any active SKIP_UNTIL command as Applied=Y, effectively ending the email pause.
    Returns True if a SKIP_UNTIL was found and cleared, False otherwise.
    """
    try:
        from memory import read_commands, mark_command_applied
        commands = read_commands()
        cleared = False
        for cmd in commands:
            if (cmd.get("Command", "").upper().strip() == "SKIP_UNTIL"
                    and cmd.get("Applied", "").upper().strip() not in ("Y", "DECLINED")):
                row_index = cmd.get("_row_index")
                if row_index:
                    mark_command_applied(row_index)
                    cleared = True
        return cleared
    except Exception as e:
        print(f"[Telegram] End-skip failed (non-fatal): {e}")
        return False


def _get_pending_proposals() -> list[dict]:
    """Return unapplied PENDING_PROPOSAL rows from Commands tab."""
    try:
        from memory import read_commands
        return [
            c for c in read_commands()
            if c.get("Command", "").upper() == "PENDING_PROPOSAL"
            and c.get("Applied", "").upper() not in ("Y", "DECLINED")
        ]
    except Exception:
        return []


def _resolve_proposal(row_index: int, decision: str, proposal_text: str) -> None:
    """Mark proposal applied/declined and log to Coach Focus."""
    try:
        from memory import mark_command_applied, append_coach_focus
        if decision == "Y":
            # mark_command_applied sets Applied = "Y"
            mark_command_applied(row_index)
            append_coach_focus("LANDMARK", f"[Confirmed via Telegram] {proposal_text}")
        else:
            # Write DECLINED directly
            from memory import _get_memory_sheet, TAB_COMMANDS, COMMANDS_HEADERS
            sheet = _get_memory_sheet()
            ws = sheet.worksheet(TAB_COMMANDS)
            applied_col = COMMANDS_HEADERS.index("Applied") + 1
            ws.update_cell(row_index, applied_col, "DECLINED")
            append_coach_focus("LANDMARK", f"[Declined via Telegram] {proposal_text}")
    except Exception as e:
        print(f"[Telegram] Proposal resolution failed (non-fatal): {e}")


async def _handle_confirmation(update: Update, user_text: str) -> bool:
    """
    Check if the message is a yes/no response to a pending proposal.
    Returns True if handled (and no further processing needed), False otherwise.
    """
    words = set(user_text.lower().split())
    is_yes = bool(words & _CONFIRM_WORDS)
    is_no = bool(words & _DECLINE_WORDS)

    if not is_yes and not is_no:
        return False

    proposals = _get_pending_proposals()
    if not proposals:
        return False  # Not a confirmation context — treat as normal message

    if len(proposals) == 1:
        p = proposals[0]
        proposal_text = p.get("Value", "")
        row_index = p.get("_row_index")

        if is_yes:
            _resolve_proposal(row_index, "Y", proposal_text)

            # Attempt write-back immediately
            wb_msg = ""
            try:
                from writeback import apply_writeback
                from config import compute_current_week, resolve_program_start_date
                current_week = compute_current_week(resolve_program_start_date())
                success, wb_result = apply_writeback(
                    proposal_text, current_week=current_week
                )
                if success:
                    wb_msg = f" Done — {wb_result}."
                else:
                    wb_msg = f" Note: couldn't auto-update the sheet ({wb_result})."
                print(f"  [WriteBack] {wb_result}")
            except Exception as e:
                wb_msg = " (Sheet update failed — please update manually.)"
                print(f"  [WriteBack] Error: {e}")

            reply = (
                f"Confirmed.{wb_msg} I'll reference this in the next email."
            )
        else:
            _resolve_proposal(row_index, "DECLINED", proposal_text)
            reply = "Understood, I won't make that change."

        await update.message.reply_text(reply)
        _log_message("OUT", reply)
        return True

    # Multiple proposals — ask which one
    proposal_list = "\n".join(
        f"{i+1}. {p.get('Value', '')[:100]}" for i, p in enumerate(proposals)
    )
    reply = (
        f"I have {len(proposals)} pending proposals. Which one are you confirming?\n\n"
        f"{proposal_list}\n\nReply with the number."
    )
    await update.message.reply_text(reply)
    _log_message("OUT", reply)
    return True


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


def _classify_intent(message: str) -> str:
    """
    Use Haiku to classify the athlete's message into one of four routing categories:
      WORKOUT  — today's session, substitutions, adaptation, specific exercises/sets/reps
      HEALTH   — nutrition, recovery, sleep, blood tests, HRV, injury, supplement questions
      PROGRAM  — structural program change requests (new block, periodization, deload week)
      GENERAL  — everything else (progress check, motivation, life context, chat)

    Returns one of: "WORKOUT" | "HEALTH" | "PROGRAM" | "GENERAL"
    Defaults to "GENERAL" on any error.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        result = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=10,
            system=(
                "You are a message router for a strength coaching app. "
                "Classify the athlete's message into exactly one category:\n"
                "WORKOUT — questions about today's session, exercise substitutions, sets/reps/weights, "
                "fatigue during training, skipping/modifying a session\n"
                "HEALTH — nutrition, recovery, sleep, blood tests, HRV, injury/pain management, supplements\n"
                "PROGRAM — requests to restructure the training program, create a new block, "
                "add a deload week, change the overall plan\n"
                "GENERAL — progress updates, checking in, motivation, life context, anything else\n\n"
                "Reply with exactly one word: WORKOUT, HEALTH, PROGRAM, or GENERAL."
            ),
            messages=[{"role": "user", "content": message}],
        )
        intent = result.content[0].text.strip().upper()
        if intent in ("WORKOUT", "HEALTH", "PROGRAM", "GENERAL"):
            return intent
        return "GENERAL"
    except Exception as e:
        print(f"[Router] Classification failed (defaulting to GENERAL): {e}")
        return "GENERAL"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await update.message.reply_text("Sorry, I only talk to my athlete.")
        return

    user_text = update.message.text.strip()
    if not user_text:
        return

    _log_message("IN", user_text)

    # Check for SKIP_UNTIL control phrases
    lower = user_text.lower()
    if any(phrase in lower for phrase in _RESUME_PHRASES):
        cleared = _end_skip_until()
        if cleared:
            reply = "Done — emails resume tonight. I'll be in touch as usual."
        else:
            reply = "No active email pause found. Emails are already running normally."
        await update.message.reply_text(reply)
        _log_message("OUT", reply)
        return

    # Check for yes/no confirmation of a pending proposal before normal routing
    if await _handle_confirmation(update, user_text):
        return

    # Show typing indicator while generating
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action="typing",
    )

    ctx = _build_bot_context()

    # Classify intent with Haiku — single fast call instead of cascading keyword checks
    intent = _classify_intent(user_text)
    print(f"[Router] Intent: {intent} | Message: {user_text[:60]}")

    if intent == "PROGRAM":
        try:
            from program_agent import respond as program_respond
            await update.message.reply_text("Thinking about your program... give me 30 seconds.")
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            response = program_respond(user_text, ctx)
            if len(response) > 4000:
                await update.message.reply_text(response[:4000])
                await update.message.reply_text(response[4000:])
            else:
                await update.message.reply_text(response)
            _log_message("OUT", response)
            return
        except Exception as e:
            print(f"[ProgramDesigner] Failed (falling back): {e}")

    elif intent == "HEALTH":
        try:
            from health_agent import respond as health_respond
            response = health_respond(user_text, ctx)
            await update.message.reply_text(response)
            _log_message("OUT", response)
            return
        except Exception as e:
            print(f"[HealthAgent] Failed (falling back): {e}")

    elif intent == "WORKOUT":
        try:
            from workout_agent import respond as workout_respond
            response = workout_respond(user_text, ctx)
            await update.message.reply_text(response)
            _log_message("OUT", response)
            return
        except Exception as e:
            print(f"[WorkoutAgent] Failed (falling back): {e}")

    # GENERAL intent or fallback from failed specialized agent
    model = _choose_model(user_text)
    response = _generate_response(user_text, ctx, model)

    await update.message.reply_text(response)
    _log_message("OUT", response)


# ---------------------------------------------------------------------------
# Health data ingestion — document (PDF) and photo uploads
# ---------------------------------------------------------------------------

async def _extract_pdf_text(file_bytes: bytes) -> str:
    """Extract text from a PDF byte string using pypdf."""
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n".join(pages).strip()
        return text if text else "(PDF had no extractable text — may be scanned image)"
    except ImportError:
        return "(pypdf not installed — cannot extract PDF text)"
    except Exception as e:
        return f"(PDF extraction failed: {e})"


async def _extract_photo_text(file_bytes: bytes) -> str:
    """
    Use Claude vision to extract health data from a photo (blood test screenshot,
    watch summary, nutrition label, etc.). Returns extracted text/values.
    """
    import base64
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    image_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")

    response = client.messages.create(
        model=SONNET_MODEL,  # vision requires Sonnet
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_b64,
                    }
                },
                {
                    "type": "text",
                    "text": (
                        "This is a health data image (blood test, watch summary, nutrition, etc.). "
                        "Extract all numerical values, markers, and relevant health information as structured text. "
                        "List every metric you can read: name, value, unit, reference range if shown. "
                        "Be thorough — the coach will use this to give personalized advice."
                    )
                }
            ]
        }]
    )
    return response.content[0].text


def _infer_file_type(caption: str, fallback: str = "health_data") -> str:
    """
    Infer what kind of health data a file/photo contains based on the caption.
    Returns a short string used as the file_type key in HealthAgent's extra_data dict.
    """
    if not caption:
        return fallback
    lower = caption.lower()
    if any(w in lower for w in ["blood", "sangre", "analítica", "analitica", "lab", "ferritin",
                                  "hemoglobin", "glucose", "cholesterol", "tsh", "creatinine"]):
        return "blood_data"
    if any(w in lower for w in ["hrv", "heart rate", "watch", "garmin", "oura", "whoop",
                                  "sleep score", "recovery", "readiness"]):
        return "hrv_data"
    if any(w in lower for w in ["food", "meal", "nutrition", "macros", "calories", "comida",
                                  "dieta", "proteína", "proteina"]):
        return "nutrition_data"
    if any(w in lower for w in ["photo", "picture", "foto", "progress", "body", "physique"]):
        return "body_photo"
    return fallback


async def _handle_health_file(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               extracted_text: str, file_type: str,
                               caption: str = "") -> None:
    """
    Common handler: given extracted text from a PDF/photo, call HealthAgent and reply.
    Also persists the raw extracted data to the Health Log for future reference.
    """
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    from datetime import date as _date
    today = str(_date.today())

    # Persist extracted data to Health Log + Coach Focus
    try:
        from memory import append_health_log, append_coach_focus
        # Store full extracted text in Notes; flag file type in the entry
        source_label = "PDF" if file_type == "blood_data" and "pdf" in file_type.lower() else file_type.upper()
        append_health_log([{
            "date": today,
            "notes": f"[{source_label} upload] {extracted_text[:500]}",
        }])
        append_coach_focus(
            "TRACKING",
            f"[Health file uploaded {today}] {file_type}: {extracted_text[:120]}",
            last_mentioned=today,
        )
        print(f"[Telegram] Health data persisted to Health Log ({len(extracted_text)} chars)")
    except Exception as e:
        print(f"[Telegram] Health data persist failed (non-fatal): {e}")

    # Use caption as the user's question (if any), otherwise a generic prompt
    user_question = caption.strip() if caption else f"Here's my {file_type} data — what do you see?"
    _log_message("IN", f"[{file_type.upper()} upload] {caption or '(no caption)'}")

    ctx = _build_bot_context()
    try:
        from health_agent import respond as health_respond
        response = health_respond(
            user_message=user_question,
            base_context=ctx,
            extra_data={file_type: extracted_text}
        )
    except Exception as e:
        response = f"Got the {file_type} but hit an error analysing it: {e}"

    await update.message.reply_text(response)
    _log_message("OUT", response)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle PDF and document uploads — treat as health data."""
    if not _is_authorized(update):
        return

    doc = update.message.document
    if not doc:
        return

    mime = doc.mime_type or ""
    caption = update.message.caption or ""

    # Only process PDFs for now — other file types get a polite redirect
    if mime != "application/pdf" and not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text(
            "I can read PDFs (blood tests, lab results, etc.). "
            "Send me a PDF and I'll analyse it. Other file types aren't supported yet."
        )
        return

    await update.message.reply_text("Reading your PDF...")
    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = await file.download_as_bytearray()
        extracted = await _extract_pdf_text(bytes(file_bytes))
        print(f"[Telegram] PDF received: {doc.file_name} ({len(extracted)} chars extracted)")
    except Exception as e:
        await update.message.reply_text(f"Couldn't download the PDF: {e}")
        return

    await _handle_health_file(update, context, extracted, _infer_file_type(caption, "pdf"), caption)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo uploads — use Claude vision to extract health data."""
    if not _is_authorized(update):
        return

    photos = update.message.photo
    if not photos:
        return

    caption = update.message.caption or ""

    await update.message.reply_text("Reading your photo...")
    try:
        # Use the largest available size
        largest = max(photos, key=lambda p: p.file_size or 0)
        file = await context.bot.get_file(largest.file_id)
        file_bytes = await file.download_as_bytearray()
        print(f"[Telegram] Photo received ({len(file_bytes)} bytes)")
        extracted = await _extract_photo_text(bytes(file_bytes))
        print(f"[Telegram] Vision extracted: {extracted[:100]}")
    except Exception as e:
        await update.message.reply_text(f"Couldn't read the photo: {e}")
        return

    await _handle_health_file(update, context, extracted, _infer_file_type(caption, "photo"), caption)


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
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("Bot running. Waiting for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
