"""
Coach Memory Sheet — the agent's persistent brain.
Reads and writes to a dedicated Google Sheet that persists across programs.

Tabs:
  Athlete Profile   - stable personal info (user edits)
  Long-Term Goals   - multi-year aspirations (user edits)
  Lift History      - append-only session log (agent writes)
  Health Log        - append-only health data (agent writes)
  Life Context      - journal of context changes (agent appends)
  Program History   - programs run (agent updates)
  Coach Log         - agent's own notes and email summaries
"""

from datetime import date, datetime
from typing import Optional

import gspread

from sheets import get_client
from config import MEMORY_SHEET_ID


# ---------------------------------------------------------------------------
# Tab names
# ---------------------------------------------------------------------------

TAB_PROFILE = "Athlete Profile"
TAB_GOALS = "Long-Term Goals"
TAB_LIFT_HISTORY = "Lift History"
TAB_HEALTH_LOG = "Health Log"
TAB_LIFE_CONTEXT = "Life Context"
TAB_PROGRAM_HISTORY = "Program History"
TAB_COACH_LOG = "Coach Log"

LIFT_HISTORY_HEADERS = ["Date", "Week", "Day", "Exercise", "Prescribed Weight",
                         "Actual Weight/Reps", "Completed", "Notes"]
HEALTH_LOG_HEADERS = ["Date", "Bodyweight (kg)", "Steps", "Sleep (hrs)",
                       "Food Quality (1-10)", "Sun (Y/N)", "Notes"]
LIFE_CONTEXT_HEADERS = ["Date", "Context"]
PROGRAM_HISTORY_HEADERS = ["Program", "Start Date", "End Date", "Weeks Completed", "Notes"]
COACH_LOG_HEADERS = ["Date", "Key Observations", "Email Summary"]


# ---------------------------------------------------------------------------
# Sheet access
# ---------------------------------------------------------------------------

def _get_memory_sheet() -> gspread.Spreadsheet:
    client = get_client()
    return client.open_by_key(MEMORY_SHEET_ID)


def _get_tab(sheet: gspread.Spreadsheet, name: str) -> gspread.Worksheet:
    try:
        return sheet.worksheet(name)
    except gspread.WorksheetNotFound:
        raise RuntimeError(
            f"Coach Memory tab '{name}' not found. "
            "Run `python src/memory.py --setup` to create the sheet structure."
        )


# ---------------------------------------------------------------------------
# Read functions
# ---------------------------------------------------------------------------

def read_athlete_profile() -> str:
    """Read Athlete Profile tab as raw text."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_PROFILE)
    rows = ws.get_all_values()
    lines = []
    for row in rows:
        line = " | ".join(str(c).strip() for c in row if c)
        if line:
            lines.append(line)
    return "\n".join(lines)


def read_long_term_goals() -> str:
    """Read Long-Term Goals tab as raw text."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_GOALS)
    rows = ws.get_all_values()
    lines = []
    for row in rows:
        line = " | ".join(str(c).strip() for c in row if c)
        if line:
            lines.append(line)
    return "\n".join(lines)


def read_lift_history(limit: int = 80) -> list[dict]:
    """Read last N rows of Lift History."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_LIFT_HISTORY)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []

    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entry = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        entries.append(entry)

    return entries[-limit:]


def read_health_log(limit: int = 30) -> list[dict]:
    """Read last N rows of Health Log."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_HEALTH_LOG)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []

    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entry = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        entries.append(entry)

    return entries[-limit:]


def read_life_context(limit: int = 10) -> list[dict]:
    """Read last N life context entries."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_LIFE_CONTEXT)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []

    entries = []
    for row in rows[1:]:
        if len(row) >= 2 and any(row):
            entries.append({"date": row[0], "context": row[1] if len(row) > 1 else ""})

    return entries[-limit:]


def read_program_history() -> list[dict]:
    """Read all program history entries."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_PROGRAM_HISTORY)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []

    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entries.append(dict(zip(headers, row + [""] * (len(headers) - len(row)))))
    return entries


def read_coach_log(limit: int = 7) -> list[dict]:
    """Read last N coach log entries."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_COACH_LOG)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []

    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entries.append(dict(zip(headers, row + [""] * (len(headers) - len(row)))))

    return entries[-limit:]


def read_all() -> dict:
    """Read all Coach Memory data at once."""
    return {
        "athlete_profile": read_athlete_profile(),
        "long_term_goals": read_long_term_goals(),
        "lift_history": read_lift_history(),
        "health_log": read_health_log(),
        "life_context": read_life_context(),
        "program_history": read_program_history(),
        "coach_log": read_coach_log(),
    }


# ---------------------------------------------------------------------------
# Write functions
# ---------------------------------------------------------------------------

def append_lift_history(sessions: list[dict]) -> None:
    """
    Append new session data to Lift History.
    Each session dict: {week, day_label, exercise_name, prescribed_weight,
                        actual, completed, notes, date}
    """
    if not sessions:
        return
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_LIFT_HISTORY)

    rows = []
    for s in sessions:
        rows.append([
            str(s.get("date", date.today())),
            str(s.get("week", "")),
            str(s.get("day_label", "")),
            str(s.get("exercise_name", "")),
            str(s.get("prescribed_weight", "")),
            str(s.get("actual", "")),
            "Y" if s.get("completed") else ("N" if s.get("completed") is False else "?"),
            str(s.get("notes", "")),
        ])

    ws.append_rows(rows)


def append_health_log(entries: list[dict]) -> None:
    """
    Append new health log entries (from Daily Log tab).
    Each entry: {date, bodyweight, steps, sleep, food_quality, sun, notes}
    """
    if not entries:
        return
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_HEALTH_LOG)

    rows = []
    for e in entries:
        sun = e.get("sun")
        sun_str = "Y" if sun is True else ("N" if sun is False else "")
        rows.append([
            str(e.get("date", "")),
            str(e.get("bodyweight", "") or ""),
            str(e.get("steps", "") or ""),
            str(e.get("sleep", "") or ""),
            str(e.get("food_quality", "") or ""),
            sun_str,
            str(e.get("notes", "") or ""),
        ])

    ws.append_rows(rows)


def append_life_context(context_note: str, context_date: Optional[date] = None) -> None:
    """Append a life context change detected from notes."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_LIFE_CONTEXT)
    d = str(context_date or date.today())
    ws.append_row([d, context_note])


def log_coach_run(observations: str, email_summary: str,
                  run_date: Optional[date] = None) -> None:
    """Log what the agent observed and sent today."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_COACH_LOG)
    d = str(run_date or date.today())
    ws.append_row([d, observations, email_summary])


def update_program_history(program_name: str, start_date: str,
                            end_date: str = "", weeks_completed: int = 0,
                            notes: str = "") -> None:
    """Add or update a program history entry."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_PROGRAM_HISTORY)
    ws.append_row([program_name, start_date, end_date, str(weeks_completed), notes])


# ---------------------------------------------------------------------------
# Sync: detect new data and append to history
# ---------------------------------------------------------------------------

def sync_sessions_to_history(program_data: dict) -> list[dict]:
    """
    Compare program data against existing Lift History to find new sessions.
    Appends new sessions and returns the list of what was synced.

    A session is "new" if it's marked Done and isn't already in Lift History
    for that date+exercise combination.
    """
    existing = read_lift_history(limit=500)
    existing_keys = set()
    for row in existing:
        key = (row.get("Date", ""), row.get("Exercise", ""))
        existing_keys.add(key)

    new_sessions = []
    current_week = program_data.get("current_week")
    if not current_week:
        return []

    week_num = current_week.get("week_num", "?")

    for day in current_week.get("days", []):
        day_label = day.get("label", "")
        session_date = day.get("date")

        for ex in day.get("exercises", []):
            if ex.get("done") is not True:
                continue

            key = (str(session_date or ""), ex["name"])
            if key in existing_keys:
                continue

            new_sessions.append({
                "date": session_date or date.today(),
                "week": week_num,
                "day_label": day_label,
                "exercise_name": ex["name"],
                "prescribed_weight": ex.get("weight", ""),
                "actual": ex.get("actual", ""),
                "completed": True,
                "notes": ex.get("notes", ""),
            })

    if new_sessions:
        append_lift_history(new_sessions)

    return new_sessions


def sync_health_log(program_data: dict) -> list[dict]:
    """
    Sync new Daily Log entries to Health Log in Coach Memory.
    Returns list of newly synced entries.
    """
    existing = read_health_log(limit=500)
    existing_dates = {row.get("Date", "") for row in existing}

    new_entries = []
    for entry in program_data.get("daily_log", []):
        date_str = str(entry.get("date", ""))
        if date_str in existing_dates:
            continue
        new_entries.append(entry)

    if new_entries:
        append_health_log(new_entries)

    return new_entries


# ---------------------------------------------------------------------------
# Setup: create the Coach Memory Sheet structure
# ---------------------------------------------------------------------------

def setup_memory_sheet() -> None:
    """
    Create all required tabs in the Coach Memory Sheet with headers.
    Safe to run multiple times — skips tabs that already exist.
    """
    sheet = _get_memory_sheet()
    existing = {ws.title for ws in sheet.worksheets()}

    def ensure_tab(name: str, headers: list[str], template_rows: list[list] = None):
        if name in existing:
            print(f"  Tab '{name}' already exists, skipping.")
            return
        ws = sheet.add_worksheet(title=name, rows=1000, cols=10)
        if headers:
            ws.append_row(headers)
        if template_rows:
            ws.append_rows(template_rows)
        print(f"  Created tab '{name}'.")

    print("Setting up Coach Memory Sheet tabs...")

    ensure_tab(TAB_PROFILE, [], [
        ["Name", "Nacho"],
        ["Age", ""],
        ["Training Since", ""],
        ["Health Conditions", "Insulin resistance (carb timing matters). Golfer's elbow (watch pull volume)."],
        ["Background", "Finance professional. Works 14-16h/day. Travels Mon-Thu every 2 weeks. Based in Spain."],
        ["Coaching Preferences", "Direct and honest. Data over motivation. No pandering. Answers questions directly."],
        ["Current Program", "30-Week Strength Program (started 2026-01-13)"],
    ])

    ensure_tab(TAB_GOALS, [], [
        ["Goal", "Notes", "Added Date"],
        ["Reach 120kg squat x5 by Week 30", "Current program target", "2026-01-13"],
        ["Reach 105kg bench x5 by Week 30", "Current program target", "2026-01-13"],
        ["Eventually incorporate Olympic weightlifting", "Long-term, not current focus", "2026-01-13"],
        ["Improve cardio base", "Lost fitness from sedentary work periods", "2026-01-13"],
    ])

    ensure_tab(TAB_LIFT_HISTORY, LIFT_HISTORY_HEADERS)
    ensure_tab(TAB_HEALTH_LOG, HEALTH_LOG_HEADERS)
    ensure_tab(TAB_LIFE_CONTEXT, LIFE_CONTEXT_HEADERS, [
        ["2026-01-13", "Started 30-week strength program. Week 7 current as of 2026-03-05."],
    ])
    ensure_tab(TAB_PROGRAM_HISTORY, PROGRAM_HISTORY_HEADERS, [
        ["30-Week Strength Program", "2026-01-13", "", "7", "In progress as of 2026-03-05"],
    ])
    ensure_tab(TAB_COACH_LOG, COACH_LOG_HEADERS)

    print("Done. Review and edit the Athlete Profile and Long-Term Goals tabs directly in Google Sheets.")


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if "--setup" in sys.argv:
        setup_memory_sheet()
    else:
        print("Reading Coach Memory...")
        data = read_all()
        print(f"\nAthlete Profile:\n{data['athlete_profile']}")
        print(f"\nLong-Term Goals:\n{data['long_term_goals']}")
        print(f"\nLift History: {len(data['lift_history'])} entries")
        print(f"Health Log: {len(data['health_log'])} entries")
        print(f"Life Context: {len(data['life_context'])} entries")
        print(f"Coach Log: {len(data['coach_log'])} entries")
