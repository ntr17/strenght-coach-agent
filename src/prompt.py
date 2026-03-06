"""
Builds the Claude prompt from all available context.
System prompt is stable. User message is assembled dynamically each run.
"""

from datetime import date, datetime
from typing import Optional

from config import ATHLETE_NAME, PROGRAM_START_DATE, compute_current_week


# ---------------------------------------------------------------------------
# System prompt (stable, loaded every run)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are {ATHLETE_NAME}'s long-term strength coach. You've been working with him for months. You know him well — his patterns, his excuses, his genuine effort, his life outside the gym.

You care about this person. Not in a sentimental way — in the way a good coach cares: you want him to succeed, you pay attention to what's actually happening with him, and you hold him to a standard because you believe he can meet it. You're not here to make him feel good. You're here to make him better.

You think in years, not weeks. You have a roadmap in your head for where he's going — the next program, the next phase after that, the target he's building toward. Today's session is one data point in a much longer story. You keep that context active.

Your coaching philosophy:
- Direct and honest. If something is wrong, you name it clearly. No softening.
- Data over motivation. You interpret numbers; you don't cheerleaded them.
- You remember where he started. When he can't see progress, you remind him with specifics.
- You notice what he misses: the pattern building across weeks, the lifestyle factor compounding against him, the thing he wrote in a note three weeks ago that's relevant now.
- You have professional opinions. You sometimes disagree with his instincts. You say so.
- You never change the program without asking. Always propose, always confirm first.
- You answer his questions woven naturally into the coaching — never in a separate block.
- If there's a proactive alert worth a quick Telegram message (plateau breaking, something urgent, a milestone), append it at the very end using this exact format on its own line: [TELEGRAM: your brief message here]

Email format:
- Natural prose. No section headers. No bullet lists unless they genuinely help.
- Length matches what's relevant: rest day with nothing to say = 2 sentences; training day with a question and a trend = several paragraphs.
- Don't recite the data. Interpret it. Tell him what it means.
- Tone: a coach who knows him well and doesn't waste his time. Warm but not gushing.

If you want to propose a program change (weight, structure, anything): state it clearly and ask. Format: "One thing: [proposal]. Want me to update the sheet?"
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
            if e.get("session_note") and e["session_note"] not in ("", "None"):
                day_notes.append(f"{e['name']} [your note]: {e['session_note']}")
            elif e.get("notes") and e["notes"] not in ("", "None"):
                # Legacy single-notes column
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
            session_note = e.get("session_note") or e.get("notes")
            if session_note:
                line += f" | your note: {session_note}"
            prog_note = e.get("program_note")
            if prog_note and not e.get("session_note"):
                # Only show program note if there's no session note (avoid clutter)
                line += f" | program: {prog_note}"
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
                # Check session note first (user's own words), then legacy notes
                note = ex.get("session_note") or ex.get("notes", "")
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
# Delta: what changed since the last email
# ---------------------------------------------------------------------------

def _format_delta(program_data: dict, last_run_date: Optional[date]) -> str:
    """
    Build a summary of what is NEW since the last coaching email.
    Covers: newly completed sessions, new daily log entries, new notes/questions.
    If last_run_date is None (first run), returns empty string.
    """
    if last_run_date is None:
        return ""

    lines = [f"Since last email ({last_run_date.strftime('%b %d')}):"]
    found_anything = False

    # New sessions from current week
    for week_key in ("current_week", "prev_week_carryover"):
        week = program_data.get(week_key)
        if not week:
            continue
        for day in week.get("days", []):
            day_date = day.get("date")
            if day_date and day_date <= last_run_date:
                continue  # session predates last email
            for ex in day.get("exercises", []):
                if ex.get("done") is not True:
                    continue
                note = ex.get("session_note") or ex.get("notes") or ""
                line = f"  ✓ {ex['name']} {ex.get('weight', '')} {ex.get('sets_reps', '')}"
                if ex.get("actual"):
                    line += f" → {ex['actual']}"
                if note:
                    line += f" | \"{note}\""
                lines.append(line)
                found_anything = True

    # New daily log entries
    new_log = []
    for entry in program_data.get("daily_log", []):
        entry_date = entry.get("date")
        if entry_date and entry_date > last_run_date:
            new_log.append(entry)

    if new_log:
        found_anything = True
        for e in new_log:
            parts = []
            if e.get("bodyweight"):
                parts.append(f"BW {e['bodyweight']}kg")
            if e.get("sleep"):
                parts.append(f"sleep {e['sleep']}h")
            if e.get("energy"):
                parts.append(f"energy {e['energy']}/10")
            if e.get("steps"):
                parts.append(f"{int(e['steps']):,} steps")
            if e.get("notes"):
                parts.append(f"\"{e['notes']}\"")
            lines.append(f"  [{e['date']}] {' | '.join(parts)}" if parts else f"  [{e['date']}] (no data)")

    if not found_anything:
        return ""

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1RM trajectory
# ---------------------------------------------------------------------------

def _format_1rm_trajectory(lift_history: list[dict]) -> str:
    """
    For each key lift, show the last 4 estimated 1RM values and flag plateaus.
    A plateau = less than 1% change across the last 3 readings.
    """
    key_lifts = ["Squat", "Bench Press", "Deadlift", "OHP"]
    lines = []

    for lift in key_lifts:
        readings = []
        for row in lift_history:
            ex_name = row.get("Exercise", "")
            if lift.lower() not in ex_name.lower():
                continue
            est = row.get("Est 1RM", "")
            date_str = row.get("Date", "")
            if not est:
                continue
            try:
                readings.append((date_str, float(est)))
            except (ValueError, TypeError):
                pass

        if not readings:
            continue

        recent = readings[-4:]  # last 4 data points
        values = [v for _, v in recent]
        pts = ", ".join(f"{v:.1f}kg ({d})" for d, v in recent)

        plateau_flag = ""
        if len(values) >= 3:
            spread = max(values[-3:]) - min(values[-3:])
            if spread / max(values[-3:]) < 0.01:
                plateau_flag = " ⚠ PLATEAU"

        lines.append(f"  {lift} est. 1RM: {pts}{plateau_flag}")

    return "\n".join(lines) if lines else "No 1RM data yet (need actual weights or sets/reps logged)."


# ---------------------------------------------------------------------------
# Rolling trends
# ---------------------------------------------------------------------------

def _compute_rolling_trends(health_log: list[dict], recent_weeks: list[dict]) -> str:
    """
    Compare last 2 weeks vs last 4 weeks for key metrics.
    Metrics: bodyweight, sleep, energy, session completion rate.
    """
    def avg_health(entries, key, n):
        vals = []
        for e in entries[-n:]:
            try:
                v = float(e.get(key, "") or "")
                vals.append(v)
            except (ValueError, TypeError):
                pass
        return round(sum(vals) / len(vals), 1) if vals else None

    lines = []

    bw_2 = avg_health(health_log, "Bodyweight (kg)", 14)
    bw_4 = avg_health(health_log, "Bodyweight (kg)", 28)
    if bw_2 and bw_4:
        diff = round(bw_2 - bw_4, 1)
        arrow = "↑" if diff > 0.2 else ("↓" if diff < -0.2 else "→")
        lines.append(f"  Bodyweight: {bw_2}kg (2wk avg) vs {bw_4}kg (4wk avg) {arrow} {diff:+.1f}kg")

    sleep_2 = avg_health(health_log, "Sleep (hrs)", 14)
    sleep_4 = avg_health(health_log, "Sleep (hrs)", 28)
    if sleep_2 and sleep_4:
        diff = round(sleep_2 - sleep_4, 1)
        arrow = "↑" if diff > 0.1 else ("↓" if diff < -0.1 else "→")
        lines.append(f"  Sleep: {sleep_2}h (2wk avg) vs {sleep_4}h (4wk avg) {arrow}")

    energy_2 = avg_health(health_log, "Food Quality (1-10)", 14)
    energy_4 = avg_health(health_log, "Food Quality (1-10)", 28)
    if energy_2 and energy_4:
        diff = round(energy_2 - energy_4, 1)
        arrow = "↑" if diff > 0.3 else ("↓" if diff < -0.3 else "→")
        lines.append(f"  Food quality: {energy_2}/10 (2wk avg) vs {energy_4}/10 (4wk avg) {arrow}")

    # Session completion rate: all_weeks from recent_weeks
    all_weeks = recent_weeks[-4:] if recent_weeks else []
    if len(all_weeks) >= 2:
        def completion_rate(weeks):
            total, done = 0, 0
            for w in weeks:
                for day in w.get("days", []):
                    for ex in day.get("exercises", []):
                        total += 1
                        if ex.get("done") is True:
                            done += 1
            return round(done / total * 100, 0) if total else None

        rate_2 = completion_rate(all_weeks[-2:])
        rate_4 = completion_rate(all_weeks)
        if rate_2 is not None and rate_4 is not None:
            diff = rate_2 - rate_4
            arrow = "↑" if diff > 3 else ("↓" if diff < -3 else "→")
            lines.append(f"  Session completion: {rate_2:.0f}% (last 2wk) vs {rate_4:.0f}% (last 4wk) {arrow}")

    return "\n".join(lines) if lines else "Not enough data for trend comparison yet."


# ---------------------------------------------------------------------------
# Main prompt builder
# ---------------------------------------------------------------------------

def _format_replies(replies: list[dict]) -> str:
    """Format email replies from the user for inclusion in the prompt."""
    if not replies:
        return ""
    lines = []
    for r in replies:
        lines.append(f"  [{r.get('date', '')}] Subject: {r.get('subject', '')}")
        body = r.get("body", "").strip()
        if body:
            # Indent the body
            for line in body.split("\n")[:10]:  # cap at 10 lines
                lines.append(f"    {line}")
    return "\n".join(lines)


def _format_active_commands(commands: list[dict]) -> str:
    """Format active (unapplied) commands for the agent's awareness."""
    active = [
        c for c in commands
        if c.get("Applied", "").upper().strip() != "Y"
        and not c.get("Command", "").startswith("#")
    ]
    if not active:
        return ""
    lines = []
    for c in active:
        line = f"  {c.get('Command', '')} | {c.get('Value', '')}"
        if c.get("Expires"):
            line += f" | expires: {c['Expires']}"
        lines.append(line)
    return "\n".join(lines)


def _format_strategic_plan(strategic_plan: list[dict]) -> str:
    """Format the strategic plan phases for inclusion in the prompt."""
    phases = [p for p in strategic_plan if not p.get("Phase", "").startswith("#")]
    if not phases:
        return ""
    today = date.today()
    lines = []
    for p in phases:
        phase_name = p.get("Phase", "?")
        start = p.get("Start Date", "?")
        end = p.get("End Date", "?")
        focus = p.get("Focus", "?")
        targets = p.get("Key Targets", "")
        notes = p.get("Notes", "")

        # Mark current phase
        current_marker = ""
        try:
            from datetime import datetime as _dt
            s = _dt.strptime(start, "%Y-%m-%d").date()
            e = _dt.strptime(end, "%Y-%m-%d").date()
            if s <= today <= e:
                current_marker = " ← CURRENT"
        except (ValueError, TypeError):
            pass

        line = f"  {phase_name} ({start} → {end}){current_marker}: {focus}"
        if targets:
            line += f" | targets: {targets}"
        if notes:
            line += f" | {notes}"
        lines.append(line)

    updated_label = f" [last updated: {phases[-1].get('Last Updated', '?')}]" if phases else ""
    return f"Phases{updated_label}:\n" + "\n".join(lines)


def _format_telegram_log(telegram_log: list[dict]) -> str:
    """Format recent Telegram messages for inclusion in the prompt."""
    if not telegram_log:
        return ""
    lines = []
    for entry in telegram_log:
        direction = entry.get("Direction", "")
        msg = entry.get("Message", "").strip()
        d = entry.get("Date", "")
        t = entry.get("Time", "")
        label = "You" if direction == "IN" else "Coach"
        lines.append(f"  [{d} {t}] {label}: {msg}")
    return "\n".join(lines)


def build_prompt(program_data: dict, memory_data: dict,
                 last_run_date: Optional[date] = None,
                 replies: list[dict] = None,
                 is_weekly_summary: bool = False,
                 plateau_deep_dives: dict = None) -> tuple[str, str]:
    """
    Build the system prompt and user message for Claude.

    Args:
        program_data: Output of sheets.read_program_data()
        memory_data: Output of memory.read_all()
        last_run_date: Date of last coaching email (from memory.get_last_run_date())
        plateau_deep_dives: Dict of {lift_name: analysis_text} for plateaued lifts

    Returns:
        (system_prompt, user_message)
    """
    today = date.today()
    week_num = program_data.get("current_week_num", compute_current_week(PROGRAM_START_DATE))
    progression = program_data.get("progression", {})
    current_week = program_data.get("current_week", {})
    prev_carryover = program_data.get("prev_week_carryover")
    recent_weeks = program_data.get("recent_weeks", [])
    daily_log = program_data.get("daily_log", [])

    # Determine block info from progression
    block_info = ""
    if week_num in progression:
        block = progression[week_num].get("block", "")
        week_type = progression[week_num].get("type", "")
        if block and week_type:
            block_info = f"Block {block} — {week_type}"
        elif block:
            block_info = f"Block {block}"

    # Coaching duration (approximate)
    try:
        start = datetime.strptime(PROGRAM_START_DATE, "%Y-%m-%d").date()
        months = (today - start).days // 30
        coaching_duration = f"{months} months" if months >= 1 else "a few weeks"
    except Exception:
        coaching_duration = "several weeks"

    sections = []

    # --- Date & week context ---
    week_label = f"Week {week_num}" + (f"/30, {block_info}" if block_info else "")
    email_type = "WEEKLY SUMMARY (include charts reference)" if is_weekly_summary else "daily email"
    sections.append(
        f"Today: {today.strftime('%A, %B %d, %Y')}\n"
        f"Program: 30-Week Strength — {week_label}\n"
        f"Coaching duration: ~{coaching_duration}\n"
        f"Email type: {email_type}"
    )

    # --- Active commands (agent awareness) ---
    commands = memory_data.get("commands", [])
    cmd_text = _format_active_commands(commands)
    if cmd_text:
        sections.append(f"ACTIVE COMMANDS (from Commands tab in Coach Memory)\n{cmd_text}")

    # --- User replies (email replies since last run) ---
    if replies:
        reply_text = _format_replies(replies)
        sections.append(f"MESSAGES FROM YOU (email replies since last coaching email)\n{reply_text}")

    # --- DELTA: what's new since last email (lead with this) ---
    delta_text = _format_delta(program_data, last_run_date)
    if delta_text:
        sections.append(f"SINCE LAST EMAIL\n{delta_text}")

    # --- Questions found in notes (surface early for Claude) ---
    questions = _extract_questions(program_data)
    if questions:
        q_text = "\n".join(f"  {q}" for q in questions)
        sections.append(f"QUESTIONS TO ADDRESS (weave answers into the email naturally)\n{q_text}")

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

    # --- Strategic plan (your internal roadmap) ---
    strategic_plan = memory_data.get("strategic_plan", [])
    if strategic_plan:
        plan_text = _format_strategic_plan(strategic_plan)
        if plan_text:
            sections.append(
                f"YOUR COACHING ROADMAP (internal — updated weekly via planning pass)\n{plan_text}\n"
                "Use this to inform today's message. Surface relevant parts naturally — only when it matters."
            )

    # --- Recent Telegram conversation ---
    telegram_log = memory_data.get("telegram_log", [])
    if telegram_log:
        tg_text = _format_telegram_log(telegram_log)
        sections.append(f"RECENT TELEGRAM CONVERSATION\n{tg_text}")

    # --- 1RM trajectory ---
    lift_history = memory_data.get("lift_history", [])
    one_rm_text = _format_1rm_trajectory(lift_history)
    sections.append(f"ESTIMATED 1RM TRAJECTORY\n{one_rm_text}")

    # --- Program trajectory (start → goal vs current) ---
    trajectory = _compute_trajectory(
        program_data.get("goals", {}), progression, week_num, lift_history
    )
    sections.append(f"PROGRAM TARGETS\n{trajectory}")

    # --- Current week (full detail) ---
    current_week_text = _format_current_week(current_week) if current_week else "No current week data."
    sections.append(f"THIS WEEK\n{current_week_text}")

    # --- Previous week carryover (if any recent sessions from last week) ---
    if prev_carryover:
        sections.append(f"PREVIOUS WEEK (carry-over / recently completed)\n{_summarize_week(prev_carryover)}")

    # --- Recent weeks for trend context ---
    if recent_weeks:
        recent_parts = [_summarize_week(w) for w in recent_weeks]
        sections.append("RECENT WEEKS\n" + "\n\n".join(recent_parts))

    # --- Rolling trends ---
    all_weeks_for_trends = recent_weeks + ([prev_carryover] if prev_carryover else [])
    health_log = memory_data.get("health_log", [])
    trends_text = _compute_rolling_trends(health_log, all_weeks_for_trends)
    sections.append(f"SHORT-TERM TRENDS (2wk vs 4wk)\n{trends_text}")

    # --- Health & lifestyle ---
    health_text = _format_health_trends(health_log, daily_log)
    sections.append(f"HEALTH & LIFESTYLE\n{health_text}")

    # --- Coach log (what was said recently) ---
    coach_log = memory_data.get("coach_log", [])
    if coach_log:
        cl_lines = [
            f"  [{e.get('Date', '')}] {e.get('Key Observations', '')}"
            for e in coach_log[-5:]
        ]
        sections.append("WHAT YOU SAID RECENTLY\n" + "\n".join(cl_lines))

    # --- Plateau deep dives (per-lift analysis when plateau detected) ---
    if plateau_deep_dives:
        dive_lines = []
        for lift, analysis in plateau_deep_dives.items():
            dive_lines.append(f"  {lift}:\n  {analysis.strip()}")
        sections.append(
            "PLATEAU DEEP DIVES (full history analysis for stalled lifts)\n" +
            "\n\n".join(dive_lines)
        )

    user_message = "\n\n---\n\n".join(sections)
    if is_weekly_summary:
        user_message += (
            "\n\n---\n\nWrite the weekly summary coaching email. "
            "This is a Friday recap: cover the full week's performance, key trends, "
            "and what to focus on next week. Charts for 1RM trajectory and training volume "
            "are attached inline — reference them naturally in the text (e.g. 'as you can see in the chart below')."
        )
    else:
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
