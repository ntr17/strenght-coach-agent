"""
Builds the Claude prompt from all available context.
System prompt is stable. User message is assembled dynamically each run.
"""

from datetime import date, datetime
from typing import Optional

from config import ATHLETE_NAME, CURRENT_WEEK


# ---------------------------------------------------------------------------
# System prompt (stable, loaded every run)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are a long-term strength coach for {ATHLETE_NAME}.

You know this person well — their training history, their life situation, their tendencies, their blind spots. You've seen them progress, stall, travel, grind, and question themselves.

Your coaching philosophy:
- Direct and honest. You never pander. If something is wrong, you say so.
- Data over motivation. You interpret numbers, not just report them.
- You remember where they started and how far they've come — you remind them when they can't see it.
- You notice patterns they miss: short-term stalls, long-term trends, lifestyle factors affecting training.
- You have your own professional criteria. You give your opinion. You might disagree with their instincts — you say so clearly.
- When you want to change the program (weights, structure, anything), you always propose it and ask for confirmation first. Never change anything silently.
- You answer questions naturally within your coaching — not in a separate Q&A block, just woven in.

Email format:
- Write in natural prose. No section headers. No bullet lists unless they genuinely help.
- Length matches what's relevant. Rest day with no news: 2-3 sentences. Heavy training day with a question and a trend: several paragraphs.
- Don't repeat the data back at them. Interpret it. Tell them what it means.
- If nothing notable happened, say less.
- Tone: like a coach who knows you well and doesn't waste your time.

If you want to propose a program change (weight adjustment, exercise swap, new block), state it clearly at the end and ask for confirmation. Format: "One thing: [proposal]. Want me to update the sheet?"
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_date(d) -> str:
    if d is None:
        return "unknown date"
    if isinstance(d, str):
        return d
    return d.strftime("%b %d")


def _fmt_optional(val, suffix="") -> str:
    if val is None:
        return "—"
    return f"{val}{suffix}"


def _compute_trajectory(goals: dict, progression: dict, current_week: int,
                         lift_history: list[dict]) -> str:
    """
    Simple trajectory for key lifts: compare recent actual performance
    to progression table targets and project forward.
    """
    key_lifts = {
        "Squat": "Squat",
        "Bench Press": "Bench",
        "OHP": "OHP",
        "Deadlift": "Deadlift",
    }

    lines = []
    for goal_name, prog_key in key_lifts.items():
        goal = goals.get(goal_name)
        if not goal:
            continue

        week_target = None
        if current_week in progression:
            week_target = progression[current_week].get(prog_key)

        # Find most recent actual from lift history
        recent_actual = None
        for row in reversed(lift_history):
            if goal_name.lower() in row.get("Exercise", "").lower():
                actual = row.get("Actual Weight/Reps", "")
                completed = row.get("Completed", "")
                if actual or completed == "Y":
                    recent_actual = row.get("Prescribed Weight", "") or actual
                    break

        line = f"{goal_name}: {goal['start']} → {goal['goal']} (30wk target)"
        if week_target:
            line += f" | Week {current_week} target: {week_target}"
        if recent_actual:
            line += f" | Last recorded: {recent_actual}"

        lines.append(line)

    return "\n".join(lines) if lines else "No trajectory data available."


def _summarize_week(week_data: dict) -> str:
    """Compact summary of a past week."""
    if not week_data:
        return "No data."

    title = week_data.get("title", f"Week {week_data.get('week_num', '?')}")
    week_type = ""
    days = week_data.get("days", [])

    parts = [title]

    for day in days:
        label = day.get("label", "")
        exercises = day.get("exercises", [])
        done = [e for e in exercises if e.get("done") is True]
        not_done = [e for e in exercises if e.get("done") is False]
        unknown = [e for e in exercises if e.get("done") is None]

        day_notes = []
        for e in exercises:
            if e.get("notes") and e["notes"] not in ("", "None"):
                day_notes.append(f"{e['name']}: {e['notes']}")
            if e.get("actual") and e["actual"] not in ("", "None"):
                day_notes.append(f"{e['name']} actual: {e['actual']}")

        status = f"{len(done)}/{len(exercises)} done"
        if not_done:
            status += f" | Missed: {', '.join(e['name'] for e in not_done)}"
        if unknown:
            status += f" | Not recorded: {', '.join(e['name'] for e in unknown)}"

        day_line = f"  {label}: {status}"
        if day.get("date"):
            day_line += f" [{_fmt_date(day['date'])}]"
        parts.append(day_line)

        if day_notes:
            for note in day_notes[:3]:  # limit to avoid bloat
                parts.append(f"    → {note}")

    wn = week_data.get("weekly_notes", {})
    footer = []
    if wn.get("bodyweight"):
        footer.append(f"BW: {wn['bodyweight']}kg")
    if wn.get("sleep"):
        footer.append(f"Sleep: {wn['sleep']}h avg")
    if wn.get("energy"):
        footer.append(f"Energy: {wn['energy']}/10")
    if wn.get("notes"):
        footer.append(f"Notes: {wn['notes']}")
    if footer:
        parts.append("  " + " | ".join(footer))

    return "\n".join(parts)


def _format_current_week(week_data: dict) -> str:
    """Detailed view of the current week so far."""
    if not week_data:
        return "No current week data."

    title = week_data.get("title", "Current week")
    lines = [title]

    for day in week_data.get("days", []):
        label = day.get("label", "")
        date_str = f" [{_fmt_date(day['date'])}]" if day.get("date") else " [date unknown]"
        exercises = day.get("exercises", [])

        all_none = all(e.get("done") is None for e in exercises)
        if all_none:
            lines.append(f"  {label}{date_str}: Not done yet")
            continue

        lines.append(f"  {label}{date_str}:")
        for e in exercises:
            done_sym = "✓" if e["done"] is True else ("✗" if e["done"] is False else "?")
            line = f"    [{done_sym}] {e['name']} {e.get('weight', '')} {e.get('sets_reps', '')}"
            if e.get("actual"):
                line += f" → actual: {e['actual']}"
            if e.get("notes"):
                line += f" | note: {e['notes']}"
            lines.append(line)

    wn = week_data.get("weekly_notes", {})
    footer = []
    if wn.get("bodyweight"):
        footer.append(f"BW: {wn['bodyweight']}kg")
    if wn.get("sleep"):
        footer.append(f"Sleep avg: {wn['sleep']}h")
    if wn.get("energy"):
        footer.append(f"Energy: {wn['energy']}/10")
    if wn.get("notes"):
        footer.append(f"Week notes: {wn['notes']}")
    if footer:
        lines.append("  " + " | ".join(footer))

    return "\n".join(lines)


def _format_health_trends(health_log: list[dict], daily_log: list[dict]) -> str:
    """Combine recent health log and daily log into trends."""
    # Merge: daily_log from program sheet + health_log from memory (deduplicated)
    all_entries = {}

    for e in health_log:
        d = e.get("Date", "")
        if d:
            all_entries[d] = e

    for e in daily_log:
        d = str(e.get("date", ""))
        if d and d not in all_entries:
            all_entries[d] = {
                "Date": d,
                "Bodyweight (kg)": str(e.get("bodyweight") or ""),
                "Steps": str(e.get("steps") or ""),
                "Sleep (hrs)": str(e.get("sleep") or ""),
                "Food Quality (1-10)": str(e.get("food_quality") or ""),
                "Sun (Y/N)": "Y" if e.get("sun") else ("N" if e.get("sun") is False else ""),
                "Notes": str(e.get("notes") or ""),
            }

    if not all_entries:
        return "No health data available."

    recent = sorted(all_entries.values(), key=lambda x: x.get("Date", ""), reverse=True)[:14]

    # Compute averages for numeric fields
    def avg(key):
        vals = []
        for e in recent:
            try:
                v = float(e.get(key, "") or "")
                vals.append(v)
            except (ValueError, TypeError):
                pass
        return round(sum(vals) / len(vals), 1) if vals else None

    lines = [f"Last {len(recent)} days:"]

    bw_avg = avg("Bodyweight (kg)")
    steps_avg = avg("Steps")
    sleep_avg = avg("Sleep (hrs)")
    food_avg = avg("Food Quality (1-10)")

    if bw_avg:
        bw_vals = [float(e.get("Bodyweight (kg)", "") or 0) for e in recent if e.get("Bodyweight (kg)")]
        bw_trend = ""
        if len(bw_vals) >= 3:
            diff = bw_vals[0] - bw_vals[-1]
            bw_trend = f" (↑ {diff:+.1f}kg)" if diff > 0.2 else (f" (↓ {diff:+.1f}kg)" if diff < -0.2 else " (stable)")
        lines.append(f"  Bodyweight avg: {bw_avg}kg{bw_trend}")

    if steps_avg:
        lines.append(f"  Steps avg: {int(steps_avg):,}/day")

    if sleep_avg:
        lines.append(f"  Sleep avg: {sleep_avg}h/night")

    if food_avg:
        lines.append(f"  Food quality avg: {food_avg}/10")

    # Recent notes / questions from daily log
    notes_found = []
    for e in recent[:7]:
        note = e.get("Notes", "")
        if note and note.strip():
            notes_found.append(f"  [{e.get('Date', '')}] {note.strip()}")
    if notes_found:
        lines.append("Recent daily notes:")
        lines.extend(notes_found)

    return "\n".join(lines)


def _extract_questions(program_data: dict) -> list[str]:
    """Find question marks in notes across the current week and daily log."""
    questions = []

    current_week = program_data.get("current_week")
    if current_week:
        for day in current_week.get("days", []):
            for ex in day.get("exercises", []):
                note = ex.get("notes", "")
                if note and "?" in note:
                    questions.append(f'[{day["label"]}, {ex["name"]}] "{note}"')
        wn = current_week.get("weekly_notes", {})
        if wn.get("notes") and "?" in wn["notes"]:
            questions.append(f'[Weekly notes] "{wn["notes"]}"')

    for entry in program_data.get("daily_log", [])[:7]:
        note = entry.get("notes", "")
        if note and "?" in note:
            questions.append(f'[Daily log {entry.get("date", "")}] "{note}"')

    return questions


# ---------------------------------------------------------------------------
# Main prompt builder
# ---------------------------------------------------------------------------

def build_prompt(program_data: dict, memory_data: dict) -> tuple[str, str]:
    """
    Build the system prompt and user message for Claude.

    Returns:
        (system_prompt, user_message)
    """
    today = date.today()
    week_num = program_data.get("current_week_num", CURRENT_WEEK)
    progression = program_data.get("progression", {})
    current_week = program_data.get("current_week", {})
    recent_weeks = program_data.get("recent_weeks", [])
    daily_log = program_data.get("daily_log", [])

    # Determine block info from progression
    block_info = ""
    if week_num in progression:
        block = progression[week_num].get("block", "")
        week_type = progression[week_num].get("type", "")
        block_info = f"Block {block} — {week_type}"

    # Coaching duration (approximate)
    from config import PROGRAM_START_DATE
    try:
        start = datetime.strptime(PROGRAM_START_DATE, "%Y-%m-%d").date()
        months = (today - start).days // 30
        coaching_duration = f"{months} months" if months >= 1 else "a few weeks"
    except Exception:
        coaching_duration = "several weeks"

    sections = []

    # --- Date & week context ---
    sections.append(
        f"Today: {today.strftime('%A, %B %d, %Y')}\n"
        f"Program: 30-Week Strength — Week {week_num}/30, {block_info}\n"
        f"Coaching duration: ~{coaching_duration}"
    )

    # --- Athlete profile ---
    profile = memory_data.get("athlete_profile", "")
    if profile:
        sections.append(f"ATHLETE PROFILE\n{profile}")

    # --- Long-term goals ---
    lt_goals = memory_data.get("long_term_goals", "")
    if lt_goals:
        sections.append(f"LONG-TERM GOALS\n{lt_goals}")

    # --- Life context ---
    life_ctx = memory_data.get("life_context", [])
    if life_ctx:
        ctx_lines = "\n".join(f"  [{c['date']}] {c['context']}" for c in life_ctx[-5:])
        sections.append(f"RECENT LIFE CONTEXT\n{ctx_lines}")

    # --- Program trajectory ---
    lift_history = memory_data.get("lift_history", [])
    trajectory = _compute_trajectory(
        program_data.get("goals", {}), progression, week_num, lift_history
    )
    sections.append(f"PROGRAM TRAJECTORY (key lifts)\n{trajectory}")

    # --- Current week ---
    current_week_text = _format_current_week(current_week) if current_week else "No current week data."
    sections.append(f"THIS WEEK\n{current_week_text}")

    # --- Recent weeks ---
    if recent_weeks:
        recent_parts = []
        for w in recent_weeks:
            recent_parts.append(_summarize_week(w))
        sections.append("RECENT WEEKS\n" + "\n\n".join(recent_parts))

    # --- Health trends ---
    health_log = memory_data.get("health_log", [])
    health_text = _format_health_trends(health_log, daily_log)
    sections.append(f"HEALTH & LIFESTYLE\n{health_text}")

    # --- Coach log (what was said recently) ---
    coach_log = memory_data.get("coach_log", [])
    if coach_log:
        cl_lines = []
        for entry in coach_log[-5:]:
            cl_lines.append(f"  [{entry.get('Date', '')}] {entry.get('Key Observations', '')}")
        sections.append("WHAT YOU SAID RECENTLY\n" + "\n".join(cl_lines))

    # --- Questions found in notes ---
    questions = _extract_questions(program_data)
    if questions:
        q_text = "\n".join(f"  {q}" for q in questions)
        sections.append(f"QUESTIONS IN NOTES (answer these naturally)\n{q_text}")

    user_message = "\n\n---\n\n".join(sections)
    user_message += "\n\n---\n\nWrite the coaching email."

    return SYSTEM_PROMPT, user_message


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    # Minimal test with mock data
    mock_program = {
        "current_week_num": 7,
        "goals": {
            "Squat": {"start": "85kg x 5", "goal": "120kg x 5", "gain": "+35kg"},
            "Bench Press": {"start": "75kg x 5", "goal": "105kg x 5", "gain": "+30kg"},
        },
        "progression": {
            7: {"Squat": "92.5kg", "Bench": "80kg", "block": 2, "type": "PROGRESS"}
        },
        "current_week": {
            "title": "WEEK 7 — Block 2",
            "week_num": 7,
            "days": [
                {
                    "label": "DAY 1: Squat + Bench Heavy",
                    "date": date.today(),
                    "exercises": [
                        {"name": "Squat", "weight": "92.5kg", "sets_reps": "4x4",
                         "done": True, "actual": None, "notes": "felt heavier than expected, should I add weight next week?"},
                        {"name": "Bench Press", "weight": "80kg", "sets_reps": "4x4",
                         "done": True, "actual": None, "notes": None},
                    ]
                }
            ],
            "weekly_notes": {"bodyweight": 82.5, "sleep": 6.5, "energy": 7, "notes": None}
        },
        "recent_weeks": [],
        "daily_log": [],
    }
    mock_memory = {
        "athlete_profile": "Name: Nacho | Health: Insulin resistance, golfer's elbow | Background: Finance, 14h/day, travels biweekly",
        "long_term_goals": "120kg squat | Eventually Olympic lifting",
        "life_context": [{"date": "2026-01-13", "context": "Started 30-week program"}],
        "lift_history": [],
        "health_log": [],
        "coach_log": [],
    }

    system_prompt, user_message = build_prompt(mock_program, mock_memory)
    print("=== SYSTEM PROMPT ===")
    print(system_prompt)
    print("\n=== USER MESSAGE ===")
    print(user_message)
