"""
ProgramDesignerAgent — designs and creates new training programs as Google Sheets.

Triggered from telegram_bot.py when the athlete requests a new program, block, or
deload. The agent reasons about current state (lifts, health, projections, goals)
and produces a structured program, then writes it to a new Google Sheet in the
athlete's Drive and registers it in the Active Sheets registry.

Trigger examples:
  "design me a deload week"
  "create a new 4-week block after this program"
  "I need a 2-week transition after vacation"
  "what should I do for the next block? my squat has been stalling"
  "new program" / "build me a program"
"""

import json
import os
import re
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))

import anthropic
import gspread

from config import ANTHROPIC_API_KEY, ATHLETE_NAME, CLAUDE_MODEL, GMAIL_TO

SONNET_MODEL = CLAUDE_MODEL

TRIGGER_KEYWORDS = [
    "new program", "new block", "next block", "design program", "build program",
    "create program", "write program", "design block", "new cycle",
    "deload", "deload week", "transition", "after this program", "after vacation",
    "next phase", "what next", "what should i do next",
    "programa", "nuevo programa", "diseña", "crea un programa",
]


# ---------------------------------------------------------------------------
# Keyword detection
# ---------------------------------------------------------------------------

def is_program_design_query(message: str) -> bool:
    """Return True if the message is requesting a program design."""
    lower = message.lower()
    return any(kw in lower for kw in TRIGGER_KEYWORDS)


# ---------------------------------------------------------------------------
# LLM design pass
# ---------------------------------------------------------------------------

_DESIGN_SYSTEM = f"""You are {ATHLETE_NAME}'s strength coach designing a new training program.

You have full context: current lifts, health state, goals, program history.
Design a specific, periodized program appropriate for the request.

Output a single valid JSON object — NO explanation, no markdown, just JSON.

Schema:
{{
  "name": "<program name, e.g. 'Deload Week', '4-Week Strength Block'>",
  "type": "deload" | "strength" | "hypertrophy" | "transition" | "custom",
  "total_weeks": <int>,
  "start_date": "<YYYY-MM-DD or 'TBD'>",
  "notes": "<brief coaching rationale, 1-2 sentences>",
  "weeks": [
    {{
      "week_num": 1,
      "theme": "<week theme, e.g. 'Volume Introduction', 'Intensity Week'>",
      "days": [
        {{
          "day_num": 1,
          "label": "<short label, e.g. 'Squat + Press Heavy'>",
          "exercises": [
            {{
              "name": "<exercise name>",
              "weight": "<weight in kg, just the number>",
              "sets_reps": "<NxN format, e.g. 4x5>",
              "notes": "<optional coaching note>"
            }}
          ]
        }}
      ]
    }}
  ]
}}

Rules:
- Be specific with weights (based on current 1RM estimates, not vague %)
- For deloads: 50-60% of working weights, reduce sets by 1-2
- For strength blocks: work up from ~80% to ~90% over the weeks
- Standard 4-day split unless athlete requested otherwise: Upper/Lower or Push/Pull/Legs/Full
- Include 4-6 exercises per day (main compound + accessories)
- Weights should be realistic given current strength levels
- For "next block" after current program: build on current week's weights
"""


def design_program(request: str, context: str) -> dict:
    """
    Ask Claude Sonnet to design a program based on the request and context.
    Returns the structured program dict (parsed JSON).
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=4000,
        system=_DESIGN_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"CURRENT CONTEXT:\n{context}\n\n"
                f"---\n\n"
                f"REQUEST: {request}\n\n"
                f"Design the program now. Output only valid JSON."
            )
        }]
    )
    raw = response.content[0].text.strip()

    # Extract JSON (may be wrapped in ```json ... ```)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError(f"Could not parse program JSON from response: {raw[:200]}")


# ---------------------------------------------------------------------------
# Google Sheet creation
# ---------------------------------------------------------------------------

def _col_letter(n: int) -> str:
    """Convert 1-indexed column number to letter (1→A, 2→B, etc.)."""
    result = ""
    while n:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def _write_week_tab(ws: gspread.Worksheet, week: dict) -> None:
    """
    Write a single week's data to a worksheet tab.
    Format matches sheets.py _parse_week_tab expectations exactly.
    """
    rows = []
    week_num = week.get("week_num", "?")
    theme = week.get("theme", "")
    rows.append([f"WEEK {week_num}" + (f" — {theme}" if theme else "")])
    rows.append([])  # blank spacer

    for day in week.get("days", []):
        day_num = day.get("day_num", "?")
        label = day.get("label", "")
        rows.append([f"DAY {day_num}: {label}"])
        # Column headers (matches _detect_exercise_columns in sheets.py)
        rows.append(["Exercise", "Weight", "Sets x Reps", "Done", "Actual", "Notes", "Session Notes"])

        for ex in day.get("exercises", []):
            name   = ex.get("name", "")
            weight = ex.get("weight", "")
            sr     = ex.get("sets_reps", "")
            note   = ex.get("notes", "")
            rows.append([name, weight, sr, "☐", "", note, ""])

        rows.append([])  # blank spacer after each day

    # Weekly Notes section (matches _parse_week_tab footer parsing)
    rows.append(["WEEKLY NOTES"])
    rows.append(["Bodyweight:", ""])
    rows.append(["Sleep:", ""])
    rows.append(["Energy:", ""])
    rows.append(["Notes:", ""])

    ws.update(f"A1:G{len(rows)}", rows)

    # Light formatting: bold the header rows (week title, day labels, col headers)
    try:
        # Bold week title (row 1)
        ws.format("A1", {"textFormat": {"bold": True, "fontSize": 12}})
        # Find and bold day label rows and column header rows
        for i, row in enumerate(rows, start=1):
            if row and str(row[0]).startswith("DAY "):
                ws.format(f"A{i}", {"textFormat": {"bold": True}})
            elif row and row[0] == "Exercise":
                ws.format(f"A{i}:G{i}", {
                    "textFormat": {"bold": True},
                    "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9}
                })
    except Exception:
        pass  # formatting is cosmetic, non-fatal


def create_program_sheet(program: dict, share_email: str = None) -> str:
    """
    Create a new Google Sheet for the program.
    Returns the sheet URL.

    The sheet structure matches what sheets.py expects:
      - One tab per week named "Week N"
      - Each tab: title row, day sections with Exercise header row, exercises, weekly notes
    """
    from sheets import get_client

    client = get_client()

    name = program.get("name", "New Program")
    total_weeks = program.get("total_weeks", 1)

    # Create new spreadsheet
    spreadsheet = client.create(f"{ATHLETE_NAME} — {name}")
    sheet_id = spreadsheet.id

    print(f"  [ProgramDesigner] Created sheet: {spreadsheet.url}")

    # Share with athlete's email so it appears in their Drive
    if share_email:
        try:
            spreadsheet.share(share_email, perm_type="user", role="writer")
            print(f"  [ProgramDesigner] Shared with {share_email}")
        except Exception as e:
            print(f"  [ProgramDesigner] Share failed (non-fatal): {e}")

    weeks = program.get("weeks", [])

    for i, week in enumerate(weeks):
        week_num = week.get("week_num", i + 1)
        tab_name = f"Week {week_num}"

        if i == 0:
            # Rename the default Sheet1
            ws = spreadsheet.sheet1
            ws.update_title(tab_name)
        else:
            ws = spreadsheet.add_worksheet(title=tab_name, rows=100, cols=10)

        _write_week_tab(ws, week)
        print(f"  [ProgramDesigner] Wrote {tab_name}")

    return spreadsheet.url


# ---------------------------------------------------------------------------
# Register in Active Sheets
# ---------------------------------------------------------------------------

def register_program(sheet_id: str, program: dict) -> None:
    """Register the new program in the Active Sheets registry (Coach Memory)."""
    try:
        from memory import _get_memory_sheet, TAB_SHEET_REGISTRY, SHEET_REGISTRY_HEADERS

        name = program.get("name", "New Program")
        total_weeks = program.get("total_weeks", 1)
        start_date = program.get("start_date", "TBD")
        notes = program.get("notes", "")
        prog_type = program.get("type", "strength")

        sheet = _get_memory_sheet()
        ws = sheet.worksheet(TAB_SHEET_REGISTRY)
        ws.append_row([
            name, sheet_id, prog_type.upper(), "PENDING",
            str(date.today()), start_date, str(total_weeks), notes
        ])
        print(f"  [ProgramDesigner] Registered '{name}' in Active Sheets")
    except Exception as e:
        print(f"  [ProgramDesigner] Registry write failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Build context for the design pass
# ---------------------------------------------------------------------------

def _build_design_context(base_context: str, memory_data: dict = None) -> str:
    """Assemble rich context for the program design LLM call."""
    sections = [base_context] if base_context else []

    if not memory_data:
        return "\n\n".join(sections)

    # Coach State (compressed domain summaries)
    coach_state = memory_data.get("coach_state", {})
    if coach_state:
        state_lines = []
        for domain, data in coach_state.items():
            summary = data.get("Summary", str(data)) if isinstance(data, dict) else str(data)
            state_lines.append(f"  {domain}: {summary}")
        sections.append("CURRENT LIFT STATE (coach summaries)\n" + "\n".join(state_lines))

    # Goals
    profile = memory_data.get("athlete_profile", "")
    goals = memory_data.get("long_term_goals", "")
    if goals:
        sections.append(f"LONG-TERM GOALS\n{str(goals)[:400]}")

    # Health
    health_log = memory_data.get("health_log", [])
    if health_log:
        recent = health_log[-7:]
        lines = []
        for e in recent:
            d = e.get("Date", "?")
            bw = e.get("Bodyweight (kg)", "")
            sleep = e.get("Sleep (hrs)", "")
            lines.append(f"  [{d}] BW:{bw}kg sleep:{sleep}h")
        sections.append("RECENT HEALTH\n" + "\n".join(lines))

    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------------------
# Main respond function
# ---------------------------------------------------------------------------

def respond(user_message: str, base_context: str, memory_data: dict = None) -> str:
    """
    Handle a program design request from the athlete.

    1. Builds rich context
    2. Asks Claude Sonnet to design the program (JSON)
    3. Creates a Google Sheet
    4. Registers in Active Sheets
    5. Returns a Telegram reply with the link + brief summary

    Returns the reply string to send to the athlete.
    """
    full_context = _build_design_context(base_context, memory_data)

    # Load memory if not passed
    if not memory_data:
        try:
            from memory import read_all
            memory_data = read_all()
            full_context = _build_design_context(base_context, memory_data)
        except Exception as e:
            print(f"[ProgramDesigner] Memory load failed (non-fatal): {e}")

    # Step 1: Design the program
    print(f"  [ProgramDesigner] Designing program for: {user_message[:80]}")
    try:
        program = design_program(user_message, full_context)
    except Exception as e:
        return f"I tried to design the program but hit an error parsing the structure: {e}. Try again or be more specific about what you want."

    name = program.get("name", "New Program")
    total_weeks = program.get("total_weeks", "?")
    notes = program.get("notes", "")
    weeks_data = program.get("weeks", [])

    print(f"  [ProgramDesigner] Designed: {name} ({total_weeks} weeks, {len(weeks_data)} week tabs)")

    # Step 2: Create the Google Sheet
    try:
        sheet_url = create_program_sheet(program, share_email=GMAIL_TO)
        # Extract sheet ID from URL for registry
        sheet_id_match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_url)
        sheet_id = sheet_id_match.group(1) if sheet_id_match else ""
    except Exception as e:
        return (
            f"I designed the program but couldn't create the sheet: {e}. "
            f"Here's the program structure:\n\n{name} ({total_weeks} weeks). {notes}"
        )

    # Step 3: Register in Active Sheets
    if sheet_id:
        register_program(sheet_id, program)

    # Step 4: Build reply
    day_count = sum(len(w.get("days", [])) for w in weeks_data)
    reply_lines = [
        f"Done. Created **{name}** ({total_weeks} week{'s' if total_weeks != 1 else ''}, {day_count} training days).",
        f"",
        f"{notes}" if notes else "",
        f"",
        f"Sheet: {sheet_url}",
        f"",
        f"Review it and confirm when you want to activate it — I'll start reading from it as your program.",
    ]
    return "\n".join(line for line in reply_lines if line is not None)
