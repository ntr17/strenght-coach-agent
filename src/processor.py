"""
Telegram Message Processor — runs at the start of each daily run.

Uses Haiku to classify unprocessed Telegram messages into structured events,
dispatches them to the appropriate memory tabs, and marks them processed.

This is how raw athlete messages become durable facts the coach can reason about.
"""

from datetime import date

import anthropic

from config import ANTHROPIC_API_KEY, ATHLETE_NAME


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

PROCESSOR_SYSTEM = f"""\
You are an information extractor for {ATHLETE_NAME}'s coaching system.

You receive raw Telegram messages from the athlete. Your job is to extract structured facts from them — not to reply, not to coach.

For each message (or cluster of related messages), output one or more structured lines.

CATEGORIES:
- SCHEDULE_CHANGE  — workout skipped, rescheduled, or upcoming disruption
- LIFE_EVENT       — travel, stress, illness, injury, life change that affects training
- PREFERENCE       — athlete feedback about coaching output (charts, email length, topics)
- WORKOUT_UNPLANNED — unplanned/spontaneous session not on the program
- LIFT_UPDATE      — athlete reports a specific weight, set, PR, or performance
- TRACK_LIFT       — athlete wants to add or remove a lift as a tracked main/auxiliary lift
                     (phrases like "track X", "add X as main lift", "start monitoring X", "drop X from main lifts")
- QUESTION         — athlete has a question or wants advice on something specific
- NOISE            — chitchat, acknowledgment, emoji-only, irrelevant

OUTPUT FORMAT (one line per extracted fact):
CATEGORY | DATE | FACT

Rules:
- DATE: use the message date if known, otherwise write "unknown"
- FACT: one concise sentence. What happened. No coaching.
- One message can produce multiple lines (e.g. a message about skipping + asking a question = 2 lines)
- NOISE lines are optional — only include them if useful to log
- Do NOT include JSON, markdown, or any other format. Plain lines only.

Examples:
SCHEDULE_CHANGE | 2026-03-07 | Athlete skipped Day 3 due to late flight from Madrid
LIFE_EVENT | 2026-03-07 | Athlete traveling Mon-Thu this week, training may be disrupted
PREFERENCE | 2026-03-06 | Athlete says weekly charts are not useful, prefers text only
WORKOUT_UNPLANNED | 2026-03-05 | Athlete did spontaneous pull day with pull-ups and rows
LIFT_UPDATE | 2026-03-07 | Athlete hit 100kg squat x3 in an unplanned session
TRACK_LIFT | 2026-03-07 | Athlete wants to track Romanian Deadlift as a main lift
TRACK_LIFT | 2026-03-07 | Athlete wants to remove Dip from tracked lifts
QUESTION | 2026-03-07 | Athlete asks whether to add calories on training days
"""


# ---------------------------------------------------------------------------
# Parse Haiku output
# ---------------------------------------------------------------------------

def _parse_processor_output(output: str) -> list[dict]:
    """
    Parse Haiku's line-by-line output into structured event dicts.
    Returns list of {category, event_date, fact}.
    """
    events = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        category = parts[0].upper()
        event_date = parts[1]
        fact = "|".join(parts[2:]).strip()  # fact may contain | characters

        valid_categories = {
            "SCHEDULE_CHANGE", "LIFE_EVENT", "PREFERENCE",
            "WORKOUT_UNPLANNED", "LIFT_UPDATE", "TRACK_LIFT", "QUESTION", "NOISE",
        }
        if category not in valid_categories:
            continue
        if not fact:
            continue

        events.append({
            "category": category,
            "event_date": event_date,
            "fact": fact,
        })
    return events


# ---------------------------------------------------------------------------
# Dispatch events to memory tabs
# ---------------------------------------------------------------------------

def _dispatch_events(events: list[dict], dry_run: bool = False) -> int:
    """
    Write extracted facts to the appropriate memory tabs.
    Returns number of events dispatched.
    """
    if not events:
        return 0

    from memory import (
        append_coach_focus,
        append_life_context,
        append_athlete_preference,
    )

    today = str(date.today())
    dispatched = 0

    for e in events:
        cat = e["category"]
        fact = e["fact"]
        event_date = e["event_date"]

        if dry_run:
            print(f"    [DRY RUN] {cat} | {event_date} | {fact}")
            dispatched += 1
            continue

        try:
            if cat == "SCHEDULE_CHANGE":
                # Log as FOLLOWUP so coach checks on next run
                append_coach_focus("FOLLOWUP", fact, last_mentioned=today)
                dispatched += 1

            elif cat == "LIFE_EVENT":
                # Goes to Life Context (permanent record) + FOLLOWUP in Coach Focus
                append_life_context(fact, event_date if event_date != "unknown" else today)
                append_coach_focus("TRACKING", f"[Life context] {fact}", last_mentioned=today)
                dispatched += 1

            elif cat == "PREFERENCE":
                # Extract category + preference from fact
                # Heuristic: first word(s) before ":" or whole thing
                pref_category = _infer_preference_category(fact)
                append_athlete_preference(pref_category, fact, source=f"Telegram {today}")
                dispatched += 1

            elif cat == "WORKOUT_UNPLANNED":
                # Flag as LANDMARK — coach evaluates in next analysis pass
                append_coach_focus("LANDMARK", f"[Unplanned session] {fact}", last_mentioned=today)
                dispatched += 1

            elif cat == "LIFT_UPDATE":
                # Flag as LANDMARK for coach awareness; coach may want to log to lift history
                append_coach_focus("LANDMARK", f"[Lift update via Telegram] {fact}", last_mentioned=today)
                dispatched += 1

            elif cat == "TRACK_LIFT":
                # Athlete wants to add/remove a tracked lift.
                # Log as FOLLOWUP (HIGH priority) so coach proposes formally next run
                # via the PENDING_PROPOSAL flow — coach confirms before touching the registry.
                append_coach_focus(
                    "FOLLOWUP",
                    f"[Lift tracking request] {fact}",
                    last_mentioned=today,
                    priority="HIGH",
                )
                dispatched += 1

            elif cat == "QUESTION":
                # Open question — track as FOLLOWUP so coach addresses it
                append_coach_focus("FOLLOWUP", f"[Athlete question] {fact}", last_mentioned=today)
                dispatched += 1

            elif cat == "NOISE":
                # Skip — not worth logging
                pass

        except Exception as exc:
            print(f"    [Processor] Dispatch failed for {cat}: {exc}")

    return dispatched


def _infer_preference_category(fact: str) -> str:
    """Infer a preference category from the fact text."""
    fact_lower = fact.lower()
    if any(w in fact_lower for w in ["chart", "graph", "visual"]):
        return "OUTPUT_CHARTS"
    if any(w in fact_lower for w in ["email", "length", "long", "short"]):
        return "OUTPUT_EMAIL"
    if any(w in fact_lower for w in ["telegram", "message", "notify"]):
        return "OUTPUT_TELEGRAM"
    if any(w in fact_lower for w in ["topic", "talk about", "mention"]):
        return "OUTPUT_TOPICS"
    return "OUTPUT"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def process_telegram_messages(dry_run: bool = False) -> int:
    """
    Read unprocessed Telegram messages, classify them with Haiku,
    dispatch extracted facts to memory, and mark messages processed.

    Returns number of messages processed.
    """
    from memory import read_telegram_unprocessed, mark_telegram_processed

    messages = read_telegram_unprocessed(limit=50)
    if not messages:
        return 0

    print(f"  Processing {len(messages)} unprocessed Telegram message(s)...")

    # Build the user message: one message per line with date/direction context
    lines = []
    for m in messages:
        direction = m.get("Direction", "IN")
        if direction != "IN":
            continue  # only process inbound messages from athlete
        msg_date = m.get("Date", "unknown")
        msg_time = m.get("Time", "")
        text = m.get("Message", "").strip()
        if not text:
            continue
        lines.append(f"[{msg_date} {msg_time}] {text}")

    if not lines:
        # All messages were outbound (coach → athlete), still mark as processed
        row_indices = [m.get("_row_index") for m in messages if m.get("_row_index")]
        if row_indices and not dry_run:
            mark_telegram_processed(row_indices)
        return 0

    user_content = "\n".join(lines)

    # Call Haiku
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            system=PROCESSOR_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        output = response.content[0].text
    except Exception as e:
        print(f"  Telegram processor call failed (non-fatal): {e}")
        return 0

    if dry_run:
        print("\n  --- TELEGRAM PROCESSOR OUTPUT ---")
        print(output)
        print("  --- END PROCESSOR OUTPUT ---\n")

    # Parse and dispatch
    events = _parse_processor_output(output)
    dispatched = _dispatch_events(events, dry_run=dry_run)

    if dispatched > 0:
        print(f"    → {dispatched} fact(s) dispatched to memory")

    # Mark all messages as processed (regardless of direction)
    if not dry_run:
        row_indices = [m.get("_row_index") for m in messages if m.get("_row_index")]
        if row_indices:
            mark_telegram_processed(row_indices)
            print(f"    → {len(row_indices)} message(s) marked processed")

    return len(messages)


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    dry = "--dry-run" in sys.argv

    print("Running Telegram processor...")
    count = process_telegram_messages(dry_run=dry)
    print(f"Done. Processed {count} message(s).")
