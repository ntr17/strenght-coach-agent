"""
Write-back executor — applies confirmed program change proposals to the Google Sheet.

Called from telegram_bot.py after the athlete confirms a PENDING_PROPOSAL.
Uses Claude Haiku to parse the proposal text into a structured operation,
then gspread to apply the cell changes directly.

Supported operations:
  WEIGHT_CHANGE    — modify weight for one exercise in a specific week
  SETS_REPS_CHANGE — modify sets/reps for one exercise
  EXERCISE_SWAP    — replace one exercise name with another
  NOTE_ADD         — add a note to an exercise row
  WEIGHT_SCALE     — scale all weights by % across one or more weeks (vacation recovery, deload)
  UNKNOWN          — parse failed or confidence too low → no change, human review needed
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

import anthropic
from config import ANTHROPIC_API_KEY, PROGRAM_SHEET_ID

HAIKU_MODEL = "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Proposal → structured operation (LLM parse)
# ---------------------------------------------------------------------------

_PARSE_SYSTEM = """You are a structured data extractor for a strength training program spreadsheet.

Given a coaching proposal, extract the intended change as a single JSON object.
Return ONLY valid JSON — no explanation, no markdown.

Schema:
{
  "operation": "WEIGHT_CHANGE" | "SETS_REPS_CHANGE" | "EXERCISE_SWAP" | "NOTE_ADD" | "WEIGHT_SCALE" | "UNKNOWN",
  "week": <int or null>,
  "day": <int or null>,
  "exercise": "<exercise name or null>",
  "old_value": "<old value as string or null>",
  "new_value": "<new value as string or null>",
  "scale_pct": <float or null>,
  "weeks_affected": [<int>, ...],
  "note_text": "<note to add or null>",
  "confidence": "HIGH" | "MEDIUM" | "LOW"
}

Rules:
- WEIGHT_CHANGE: single exercise, new weight. "new_value" is the new weight (just the number, e.g. "82.5").
- SETS_REPS_CHANGE: single exercise, new sets/reps. "new_value" is like "4x4" or "3x5".
- EXERCISE_SWAP: replace one exercise with another. "exercise" = old name, "new_value" = new name.
- NOTE_ADD: add a note to an exercise row. "note_text" = the note.
- WEIGHT_SCALE: scale all weights by a percentage across multiple weeks. "scale_pct" = percentage (90 = 90% of current). "weeks_affected" = list of week numbers.
- UNKNOWN: if you cannot determine the operation or key fields with confidence.

Examples:
  "Reduce squat from 90kg to 82.5kg in Week 9 Day 1"
  → {"operation":"WEIGHT_CHANGE","week":9,"day":1,"exercise":"Squat","old_value":"90","new_value":"82.5","confidence":"HIGH"}

  "Scale all weights down by 10% for weeks 9 and 10 to ease back in after vacation"
  → {"operation":"WEIGHT_SCALE","scale_pct":90.0,"weeks_affected":[9,10],"confidence":"HIGH"}

  "Swap RDL for Romanian Deadlift in Week 9 Day 3"
  → {"operation":"EXERCISE_SWAP","week":9,"day":3,"exercise":"RDL","new_value":"Romanian Deadlift","confidence":"HIGH"}

  "Change bench press sets to 3x5 in Week 9"
  → {"operation":"SETS_REPS_CHANGE","week":9,"exercise":"Bench Press","new_value":"3x5","confidence":"HIGH"}
"""


def parse_proposal(proposal_text: str, current_week: int = None) -> dict:
    """
    Parse a proposal text string into a structured operation dict.
    Optionally provide current_week to help LLM resolve relative references.
    """
    context = ""
    if current_week:
        context = f"Current week: {current_week}.\n"

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=300,
        system=_PARSE_SYSTEM,
        messages=[{"role": "user", "content": f"{context}Proposal: {proposal_text}"}]
    )
    raw = response.content[0].text.strip()

    # Extract JSON from response (may be wrapped in ```json ... ```)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return {"operation": "UNKNOWN", "confidence": "LOW", "_raw": raw}


# ---------------------------------------------------------------------------
# Sheet helpers
# ---------------------------------------------------------------------------

def _get_week_tab(sheet, week_num: int):
    """Try standard week tab name formats."""
    for name in [f"Week {week_num}", f"Semana {week_num}", f"W{week_num}"]:
        try:
            return sheet.worksheet(name)
        except Exception:
            pass
    return None


def _build_col_map_from_header(header_row: list) -> dict:
    """
    Build a field → 1-indexed column number map from a header row.
    Returns dict with keys: exercise, weight, sets_reps, done, actual, notes, session_note
    """
    col_map = {}
    for i, cell in enumerate(header_row):
        label = str(cell).strip().lower()
        if not label:
            continue
        col = i + 1  # gspread is 1-indexed
        if label == "exercise":
            col_map["exercise"] = col
        elif label in ("weight", "load"):
            col_map["weight"] = col
        elif "set" in label or "rep" in label or "x rep" in label:
            col_map["sets_reps"] = col
        elif label in ("done", "completed", "status"):
            col_map["done"] = col
        elif "actual" in label:
            col_map["actual"] = col
        elif any(kw in label for kw in ("session note", "athlete note", "my note")):
            col_map["session_note"] = col
        elif label in ("notes", "note") and "notes" not in col_map:
            col_map["notes"] = col
    return col_map


def _find_exercise_row(all_values: list, exercise_name: str, day_num: int = None) -> tuple:
    """
    Find the row index (1-based) and col_map for an exercise in a week tab.
    If day_num is specified, only match within that day's section.
    Returns (row_1based, col_map) or (None, None).
    """
    exercise_lower = exercise_name.lower().strip()
    current_day = None
    col_map = None

    for i, row in enumerate(all_values):
        if not row:
            continue
        col0 = str(row[0]).strip() if row[0] else ""
        col0_lower = col0.lower()

        # Detect day section headers (e.g. "DAY 1:", "Day 2 — Bench + Squat")
        day_match = re.match(r"day\s*(\d+)", col0_lower)
        if day_match:
            current_day = int(day_match.group(1))
            col_map = None  # reset col_map for new day
            continue

        # Detect the Exercise column header row for this day
        if col0_lower == "exercise" or (len(row) > 1 and str(row[0]).strip().lower() == "exercise"):
            col_map = _build_col_map_from_header(row)
            continue

        # Skip if we're in the wrong day
        if day_num is not None and current_day != day_num:
            continue

        # Skip if no col_map yet (haven't seen header)
        if col_map is None:
            continue

        # Check if this row is the exercise we want
        if col0_lower and exercise_lower in col0_lower:
            return i + 1, col_map  # gspread 1-indexed row

    return None, None


# ---------------------------------------------------------------------------
# Operation implementations
# ---------------------------------------------------------------------------

def _apply_weight_change(sheet, op: dict) -> tuple:
    week = op.get("week")
    exercise = op.get("exercise")
    new_val = op.get("new_value")

    if not all([week, exercise, new_val]):
        return False, "Missing week, exercise, or new_value for WEIGHT_CHANGE"

    ws = _get_week_tab(sheet, week)
    if not ws:
        return False, f"Week {week} tab not found in sheet"

    all_values = ws.get_all_values()
    row_idx, col_map = _find_exercise_row(all_values, exercise, op.get("day"))
    if not row_idx:
        return False, f"Exercise '{exercise}' not found in Week {week}"

    weight_col = col_map.get("weight")
    if not weight_col:
        return False, "Weight column not found"

    ws.update_cell(row_idx, weight_col, new_val)
    return True, f"Updated {exercise} weight to {new_val}kg in Week {week}"


def _apply_sets_reps_change(sheet, op: dict) -> tuple:
    week = op.get("week")
    exercise = op.get("exercise")
    new_val = op.get("new_value")

    if not all([week, exercise, new_val]):
        return False, "Missing week, exercise, or new_value for SETS_REPS_CHANGE"

    ws = _get_week_tab(sheet, week)
    if not ws:
        return False, f"Week {week} tab not found"

    all_values = ws.get_all_values()
    row_idx, col_map = _find_exercise_row(all_values, exercise, op.get("day"))
    if not row_idx:
        return False, f"Exercise '{exercise}' not found in Week {week}"

    sets_col = col_map.get("sets_reps")
    if not sets_col:
        return False, "Sets/Reps column not found"

    ws.update_cell(row_idx, sets_col, new_val)
    return True, f"Updated {exercise} to {new_val} in Week {week}"


def _apply_exercise_swap(sheet, op: dict) -> tuple:
    week = op.get("week")
    old_exercise = op.get("exercise")
    new_exercise = op.get("new_value")

    if not all([week, old_exercise, new_exercise]):
        return False, "Missing week, exercise, or new_value for EXERCISE_SWAP"

    ws = _get_week_tab(sheet, week)
    if not ws:
        return False, f"Week {week} tab not found"

    all_values = ws.get_all_values()
    row_idx, col_map = _find_exercise_row(all_values, old_exercise, op.get("day"))
    if not row_idx:
        return False, f"Exercise '{old_exercise}' not found in Week {week}"

    exercise_col = col_map.get("exercise", 1)
    ws.update_cell(row_idx, exercise_col, new_exercise)
    return True, f"Swapped '{old_exercise}' → '{new_exercise}' in Week {week}"


def _apply_note_add(sheet, op: dict) -> tuple:
    week = op.get("week")
    exercise = op.get("exercise")
    note = op.get("note_text") or op.get("new_value")

    if not all([week, note]):
        return False, "Missing week or note_text for NOTE_ADD"

    ws = _get_week_tab(sheet, week)
    if not ws:
        return False, f"Week {week} tab not found"

    all_values = ws.get_all_values()

    if exercise:
        row_idx, col_map = _find_exercise_row(all_values, exercise, op.get("day"))
        if row_idx:
            notes_col = col_map.get("session_note") or col_map.get("notes")
            if notes_col:
                ws.update_cell(row_idx, notes_col, note)
                return True, f"Added note to {exercise} in Week {week}"

    return False, "Could not locate a notes cell for the note"


def _apply_weight_scale(sheet, op: dict) -> tuple:
    """
    Scale all weights by scale_pct% across specified weeks.
    Weights are rounded to the nearest 2.5kg (standard plate increment).
    Used for vacation recovery, deloads, easing back in.
    """
    scale_pct = op.get("scale_pct")
    weeks = op.get("weeks_affected") or []
    if op.get("week") and not weeks:
        weeks = [op["week"]]

    if not scale_pct or not weeks:
        return False, "Missing scale_pct or weeks_affected for WEIGHT_SCALE"

    scale = scale_pct / 100.0
    total_updated = 0
    errors = []

    for week_num in weeks:
        ws = _get_week_tab(sheet, week_num)
        if not ws:
            errors.append(f"Week {week_num} tab not found")
            continue

        all_values = ws.get_all_values()
        col_map = None

        for i, row in enumerate(all_values):
            if not row:
                continue
            col0 = str(row[0]).strip().lower() if row[0] else ""

            # Detect exercise header row
            if col0 == "exercise":
                col_map = _build_col_map_from_header(row)
                continue

            # Skip non-exercise rows
            if col_map is None or not col0 or col0 in ("exercise",):
                continue

            # Skip section headers (day labels, notes section)
            if re.match(r"day\s*\d", col0) or "weekly notes" in col0 or "bodyweight" in col0:
                continue

            weight_col = col_map.get("weight")
            if not weight_col:
                continue

            # Get current weight value
            weight_cell = row[weight_col - 1] if len(row) >= weight_col else ""
            weight_str = str(weight_cell).strip()
            if not weight_str:
                continue

            # Extract numeric weight (handles "90kg", "90.0", "90")
            weight_match = re.search(r"(\d+(?:[.,]\d+)?)", weight_str)
            if not weight_match:
                continue

            try:
                weight_val = float(weight_match.group(1).replace(",", "."))
                if weight_val <= 0:
                    continue
                # Round to nearest 2.5kg
                new_weight = round(round(weight_val * scale / 2.5) * 2.5, 1)
                ws.update_cell(i + 1, weight_col, str(new_weight))
                total_updated += 1
            except (ValueError, TypeError):
                continue

    msg = f"Scaled {total_updated} weights to {scale_pct}% across weeks {weeks}"
    if errors:
        msg += f" (warnings: {'; '.join(errors)})"
    return total_updated > 0, msg


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def apply_writeback(proposal_text: str, current_week: int = None,
                    program_sheet_id: str = None) -> tuple:
    """
    Parse a proposal text and apply the change to the program sheet.

    Returns (success: bool, message: str).
    The message is human-readable and safe to send to the athlete via Telegram.
    """
    if not program_sheet_id:
        program_sheet_id = PROGRAM_SHEET_ID

    if not program_sheet_id:
        return False, "Program sheet not configured (PROGRAM_SHEET_ID missing)"

    # Step 1: Parse the proposal
    operation = parse_proposal(proposal_text, current_week=current_week)
    op_type = operation.get("operation", "UNKNOWN")
    confidence = operation.get("confidence", "LOW")

    print(f"  [WriteBack] Parsed operation: {op_type} (confidence: {confidence})")

    if op_type == "UNKNOWN" or confidence == "LOW":
        return False, (
            f"Couldn't map this proposal to a specific cell change "
            f"(op={op_type}, confidence={confidence}). "
            f"I've marked it confirmed in the log — please update the sheet manually if needed."
        )

    # Step 2: Open program sheet
    try:
        from sheets import get_client
        client = get_client()
        sheet = client.open_by_key(program_sheet_id)
    except Exception as e:
        return False, f"Could not open program sheet: {e}"

    # Step 3: Apply operation
    try:
        if op_type == "WEIGHT_CHANGE":
            return _apply_weight_change(sheet, operation)
        elif op_type == "SETS_REPS_CHANGE":
            return _apply_sets_reps_change(sheet, operation)
        elif op_type == "EXERCISE_SWAP":
            return _apply_exercise_swap(sheet, operation)
        elif op_type == "NOTE_ADD":
            return _apply_note_add(sheet, operation)
        elif op_type == "WEIGHT_SCALE":
            return _apply_weight_scale(sheet, operation)
        else:
            return False, f"Unsupported operation type: {op_type}"
    except Exception as e:
        return False, f"Write-back error ({op_type}): {e}"
