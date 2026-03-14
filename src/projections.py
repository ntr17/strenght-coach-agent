"""
Projection Engine — pure Python math, no LLM calls.

Computes forward projections for key metrics: 1RM per lift, bodyweight,
and program completion. Results are facts injected into the coaching prompt
so Claude interprets them, not hallucinate them.

All functions return structured dicts. format_projections_for_prompt()
converts them into a compact text block ready for prompt injection.
"""

import re
from datetime import date, datetime, timedelta
from typing import Optional

from config import KEY_LIFTS  # fallback when memory not available


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """
    Simple least-squares linear regression. Returns (slope, intercept).
    slope = units-of-y per unit-of-x (e.g. kg per week).
    """
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xx = sum(x * x for x in xs)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return 0.0, sum_y / n
    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def _parse_date(date_str: str) -> Optional[date]:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_str[:10], fmt).date()
        except (ValueError, TypeError):
            pass
    return None


def _weeks_since(reference_date: date, target_date: date) -> float:
    return (target_date - reference_date).days / 7.0


# ---------------------------------------------------------------------------
# 1RM Projection
# ---------------------------------------------------------------------------

def _exercise_matches(exercise_name: str, row_exercise: str) -> bool:
    """
    Match exercise_name against a Lift History row exercise name.

    Uses word-boundary matching at the start of the exercise name so that
    "Squat" matches "Squat" and "Squat (Volume)" but NOT "Front Squat".
    The match pattern must appear at the beginning of the exercise name
    (case-insensitive), followed by end-of-string or a non-word character.

    Examples:
        "Squat"      matches "Squat", "Squat (Volume)", "Squat Heavy"
        "Squat"      does NOT match "Front Squat", "Goblet Squat"
        "Bench Press" matches "Bench Press", "Bench Press (Close Grip)"
        "Bench Press" does NOT match "Dumbbell Bench Press"
    """
    pattern = r"(?i)^" + re.escape(exercise_name) + r"(\s|$|\()"
    return bool(re.match(pattern, row_exercise.strip()))


def project_1rm(
    exercise_name: str,
    lift_history: list[dict],
    target_1rm: float = None,
    weeks_remaining: int = None,
    window_days: int = 42,
) -> Optional[dict]:
    """
    Project estimated 1RM forward for a given exercise.

    Args:
        exercise_name: e.g. "Squat", "Bench Press"
        lift_history: rows from Lift History tab (must have Est 1RM + Date fields)
        target_1rm: goal 1RM in kg (optional — for on-track assessment)
        weeks_remaining: weeks left in program (optional)
        window_days: only include readings from the last N days (default 42 = 6 weeks)

    Matching: word-boundary at start — "Squat" matches "Squat (Volume)" but NOT "Front Squat".

    Current 1RM: MAX in window — intentionally lighter sessions don't pull the estimate down.
    Trend (slope): linear regression over deduplicated weekly-max readings in window.

    Returns dict with:
        exercise, current_1rm, rate_per_week, projected_end_1rm,
        on_track (bool|None), weeks_to_target (float|None), data_points
    Returns None if insufficient data (<2 unique-date readings).
    """
    cutoff = date.today() - timedelta(days=window_days)

    raw = []
    for row in lift_history:
        if not _exercise_matches(exercise_name, row.get("Exercise", "")):
            continue
        est = row.get("Est 1RM", "")
        date_str = row.get("Date", "")
        if not est or not date_str:
            continue
        try:
            val = float(str(est).replace(",", "."))
            d = _parse_date(date_str)
            if d and val > 0 and d >= cutoff:
                raw.append((d, val))
        except (ValueError, TypeError):
            pass

    if not raw:
        return None

    # Deduplicate: keep max 1RM per date (multiple sessions same day → take best)
    by_date: dict[date, float] = {}
    for d, val in raw:
        by_date[d] = max(by_date.get(d, 0.0), val)

    readings = sorted(by_date.items())  # [(date, max_val), ...]

    if len(readings) < 2:
        return None

    # Use last 8 readings for trend (recent trajectory matters more)
    recent = readings[-8:]
    reference_date = recent[0][0]
    xs = [_weeks_since(reference_date, d) for d, _ in recent]
    ys = [v for _, v in recent]

    slope, intercept = _linear_regression(xs, ys)
    # Use MAX in window as current 1RM — intentionally lighter sessions shouldn't pull it down.
    # The regression slope reflects the trend; the max reflects actual capability.
    current_1rm = max(v for _, v in recent)
    latest_date = recent[-1][0]  # most recent date for projection anchor

    projected_end = None
    if weeks_remaining is not None:
        current_x = _weeks_since(reference_date, latest_date)
        projected_end = round(slope * (current_x + weeks_remaining) + intercept, 1)

    on_track = None
    weeks_to_target = None
    if target_1rm is not None and slope > 0:
        current_x = _weeks_since(reference_date, latest_date)
        weeks_to_target = (target_1rm - current_1rm) / slope if slope > 0 else None
        if weeks_to_target is not None:
            on_track = (weeks_remaining is None) or (weeks_to_target <= weeks_remaining)

    return {
        "exercise": exercise_name,
        "current_1rm": round(current_1rm, 1),
        "rate_per_week": round(slope, 2),
        "projected_end_1rm": projected_end,
        "target_1rm": target_1rm,
        "on_track": on_track,
        "weeks_to_target": round(weeks_to_target, 1) if weeks_to_target is not None else None,
        "data_points": len(readings),
        "trend_weeks": len(recent),
    }


# ---------------------------------------------------------------------------
# Bodyweight Projection
# ---------------------------------------------------------------------------

def project_bodyweight(
    health_log: list[dict],
    target_bw: float = None,
) -> Optional[dict]:
    """
    Project bodyweight trend forward.

    Returns dict with:
        current_bw, rate_per_week, trend_direction, target_date (if target set),
        weeks_to_target (if target set + meaningful trend), data_points
    Returns None if insufficient data.
    """
    readings = []
    for row in health_log:
        bw = row.get("Bodyweight (kg)", "")
        date_str = row.get("Date", "")
        if not bw or not date_str:
            continue
        try:
            val = float(str(bw).replace(",", "."))
            d = _parse_date(date_str)
            if d and val > 0:
                readings.append((d, val))
        except (ValueError, TypeError):
            pass

    if len(readings) < 3:
        return None

    readings.sort(key=lambda r: r[0])
    recent = readings[-30:]  # last 30 data points
    reference_date = recent[0][0]
    xs = [_weeks_since(reference_date, d) for d, _ in recent]
    ys = [v for _, v in recent]

    slope, _ = _linear_regression(xs, ys)
    current_bw = recent[-1][1]

    if abs(slope) < 0.05:
        trend_direction = "stable"
    elif slope > 0:
        trend_direction = "increasing"
    else:
        trend_direction = "decreasing"

    target_date = None
    weeks_to_target = None
    if target_bw is not None and slope != 0:
        current_x = _weeks_since(reference_date, recent[-1][0])
        wks = (target_bw - current_bw) / slope
        if 0 < wks < 104:  # only meaningful if within 2 years
            weeks_to_target = round(wks, 1)
            target_date = str(recent[-1][0] + timedelta(weeks=wks))

    return {
        "current_bw": round(current_bw, 1),
        "rate_per_week": round(slope, 2),
        "trend_direction": trend_direction,
        "target_bw": target_bw,
        "target_date": target_date,
        "weeks_to_target": weeks_to_target,
        "data_points": len(readings),
        "2wk_avg": round(sum(v for _, v in readings[-14:]) / min(len(readings), 14), 1),
        "4wk_avg": round(sum(v for _, v in readings[-28:]) / min(len(readings), 28), 1),
    }


# ---------------------------------------------------------------------------
# Program Completion
# ---------------------------------------------------------------------------

def project_program_completion(
    start_date: str,
    total_weeks: int,
    today: date = None,
) -> Optional[dict]:
    """
    Compute program completion status and project end date.

    Returns dict with:
        week_num, total_weeks, pct_complete, weeks_remaining,
        estimated_end_date, days_to_end
    Returns None if start_date invalid or total_weeks <= 0.
    """
    if not start_date or not total_weeks or total_weeks <= 0:
        return None

    start = _parse_date(start_date)
    if not start:
        return None

    if today is None:
        today = date.today()

    days_elapsed = (today - start).days
    import math
    week_num = max(1, math.ceil((days_elapsed + 1) / 7))
    weeks_remaining = max(0, total_weeks - week_num)
    pct_complete = round(min(week_num / total_weeks * 100, 100), 1)
    end_date = start + timedelta(weeks=total_weeks)
    days_to_end = (end_date - today).days

    return {
        "week_num": week_num,
        "total_weeks": total_weeks,
        "pct_complete": pct_complete,
        "weeks_remaining": weeks_remaining,
        "estimated_end_date": str(end_date),
        "days_to_end": days_to_end,
    }


# ---------------------------------------------------------------------------
# Format for prompt injection
# ---------------------------------------------------------------------------

def format_projections_for_prompt(
    lift_projections: list[dict],
    bw_projection: Optional[dict],
    program_projection: Optional[dict],
) -> str:
    """
    Convert computed projections into a compact text block for prompt injection.
    These are factual numbers — Claude interprets what they mean.
    """
    lines = []

    if program_projection:
        p = program_projection
        lines.append(
            f"Program: Week {p['week_num']}/{p['total_weeks']} "
            f"({p['pct_complete']}% complete, {p['weeks_remaining']} weeks left, "
            f"ends {p['estimated_end_date']})"
        )

    for proj in lift_projections:
        if not proj:
            continue
        ex = proj["exercise"]
        curr = proj["current_1rm"]
        rate = proj["rate_per_week"]
        rate_str = f"{rate:+.2f}kg/wk"

        line = f"{ex}: {curr}kg est. 1RM | trend {rate_str}"

        if proj.get("target_1rm"):
            line += f" | target {proj['target_1rm']}kg"

        if proj.get("projected_end_1rm") is not None:
            line += f" | projected at end: {proj['projected_end_1rm']}kg"

        if proj.get("on_track") is True:
            line += " | ON TRACK"
        elif proj.get("on_track") is False:
            wtt = proj.get("weeks_to_target")
            wr = program_projection["weeks_remaining"] if program_projection else None
            if wtt and wr:
                line += f" | BEHIND ({wtt:.0f}wk needed, {wr}wk left)"
            else:
                line += " | BEHIND TARGET"

        lines.append(line)

    if bw_projection:
        bw = bw_projection
        line = (
            f"Bodyweight: {bw['current_bw']}kg | "
            f"trend {bw['rate_per_week']:+.2f}kg/wk ({bw['trend_direction']}) | "
            f"2wk avg {bw['2wk_avg']}kg vs 4wk avg {bw['4wk_avg']}kg"
        )
        if bw.get("target_bw") and bw.get("target_date"):
            line += f" | target {bw['target_bw']}kg projected {bw['target_date']}"
        lines.append(line)

    return "\n".join(lines) if lines else ""


# ---------------------------------------------------------------------------
# Convenience: run all projections from memory data
# ---------------------------------------------------------------------------

def run_all_projections(
    memory_data: dict,
    program_info: dict = None,
) -> dict:
    """
    Run all projections from memory_data dict (output of memory.read_all()).
    program_info: optional dict with {start_date, total_weeks} from registry.
    Returns: {lift_projections, bw_projection, program_projection, formatted_text}
    """
    lift_history = memory_data.get("lift_history", [])
    health_log = memory_data.get("health_log", [])

    # Determine program info
    if program_info is None:
        registry = memory_data.get("sheet_registry", [])
        for entry in registry:
            if entry.get("Type") == "Program" and entry.get("Status", "").lower() == "active":
                program_info = {
                    "start_date": entry.get("Start Date", ""),
                    "total_weeks": int(entry.get("Total Weeks", 0) or 0),
                }
                break

    # Program completion
    program_proj = None
    weeks_remaining = None
    if program_info:
        program_proj = project_program_completion(
            program_info.get("start_date", ""),
            program_info.get("total_weeks", 0),
        )
        if program_proj:
            weeks_remaining = program_proj["weeks_remaining"]

    # Try to extract targets from long-term goals text
    goals_text = memory_data.get("long_term_goals", "")
    goal_map = _parse_lift_targets(goals_text)

    # Get tracked lifts from memory_data (dynamic registry) — MAIN lifts only for projections
    tracked_lifts = memory_data.get("tracked_lifts")
    if tracked_lifts:
        lifts_for_proj = [(tl["domain"], tl["match_pattern"]) for tl in tracked_lifts
                          if tl.get("lift_type", "MAIN") == "MAIN"]
    else:
        lifts_for_proj = KEY_LIFTS

    lift_projections = []
    for _domain, lift_name in lifts_for_proj:
        target = goal_map.get(lift_name.lower())
        proj = project_1rm(lift_name, lift_history, target_1rm=target,
                           weeks_remaining=weeks_remaining)
        if proj:
            lift_projections.append(proj)

    # Bodyweight
    bw_proj = project_bodyweight(health_log)

    formatted = format_projections_for_prompt(lift_projections, bw_proj, program_proj)

    return {
        "lift_projections": lift_projections,
        "bw_projection": bw_proj,
        "program_projection": program_proj,
        "formatted": formatted,
    }


def _parse_lift_targets(goals_text: str) -> dict:
    """
    Extract target 1RM values from free-form goals text.
    E.g. "120kg squat" → {"squat": 120.0}
    Simple heuristic — not exhaustive.
    """
    targets = {}
    patterns = [
        (r"(\d+(?:\.\d+)?)\s*kg\s+squat", "squat"),
        (r"(\d+(?:\.\d+)?)\s*kg\s+bench", "bench press"),
        (r"(\d+(?:\.\d+)?)\s*kg\s+deadlift", "deadlift"),
        (r"(\d+(?:\.\d+)?)\s*kg\s+ohp", "ohp"),
        (r"(\d+(?:\.\d+)?)\s*kg\s+overhead", "ohp"),
        (r"squat[^\d]{0,10}(\d+(?:\.\d+)?)\s*kg", "squat"),
        (r"bench[^\d]{0,10}(\d+(?:\.\d+)?)\s*kg", "bench press"),
        (r"deadlift[^\d]{0,10}(\d+(?:\.\d+)?)\s*kg", "deadlift"),
    ]
    for pattern, lift in patterns:
        m = re.search(pattern, goals_text, re.IGNORECASE)
        if m and lift not in targets:
            try:
                targets[lift] = float(m.group(1))
            except (ValueError, IndexError):
                pass
    return targets


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    # Quick self-test with synthetic data
    from datetime import timedelta

    today = date.today()
    mock_history = []
    for i in range(12):
        d = today - timedelta(weeks=11 - i)
        mock_history.append({
            "Date": str(d),
            "Exercise": "Squat",
            "Est 1RM": str(80.0 + i * 1.5),
        })
        mock_history.append({
            "Date": str(d),
            "Exercise": "Bench Press",
            "Est 1RM": str(70.0 + i * 0.8),
        })

    mock_health = []
    for i in range(30):
        d = today - timedelta(days=29 - i)
        mock_health.append({
            "Date": str(d),
            "Bodyweight (kg)": str(82.0 + i * 0.02),
        })

    squat = project_1rm("Squat", mock_history, target_1rm=120.0, weeks_remaining=22)
    bench = project_1rm("Bench Press", mock_history, target_1rm=105.0, weeks_remaining=22)
    bw = project_bodyweight(mock_health)
    program = project_program_completion("2026-01-13", 30)

    formatted = format_projections_for_prompt([squat, bench], bw, program)
    print("=== PROJECTIONS ===")
    print(formatted)
    print("\nRaw Squat projection:", squat)
    print("Raw BW projection:", bw)
    print("Program completion:", program)
