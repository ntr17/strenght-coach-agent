"""
Main entry point for the strength coach agent.

Usage:
  python src/run_coach.py              # Full run: analyze + send email
  python src/run_coach.py --dry-run    # Analyze + print email, do not send
  python src/run_coach.py --week 8     # Override current week number
  python src/run_coach.py --setup      # Set up Coach Memory Sheet (first-time only)
  python src/run_coach.py --no-sync    # Skip writing new data to Coach Memory (read-only)
  python src/run_coach.py --weekly     # Force a weekly summary email with charts
  python src/run_coach.py --think      # Run strategic planning pass only (no email)
"""

import argparse
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8")

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, KEY_LIFTS, compute_current_week, resolve_program_start_date


# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Strength Coach Agent")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print email to terminal, do not send")
    parser.add_argument("--week", type=int, default=None,
                        help="Override current week number")
    parser.add_argument("--setup", action="store_true",
                        help="Set up Coach Memory Sheet structure and exit")
    parser.add_argument("--no-sync", action="store_true",
                        help="Skip writing new data to Coach Memory (read-only run)")
    parser.add_argument("--weekly", action="store_true",
                        help="Force a weekly summary email with charts (normally auto on Sundays)")
    parser.add_argument("--think", action="store_true",
                        help="Run strategic planning pass only — updates Coach Memory, no email sent")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Claude calls
# ---------------------------------------------------------------------------

def generate_analysis(system_prompt: str, user_message: str) -> str:
    """
    First pass: ask Claude to classify events, check open follow-ups, and set today's agenda.
    This is the 'thinking' step — it doesn't go in the email, it informs it.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    analysis_request = (
        "Before writing the coaching email, do this structured pre-analysis:\n\n"
        "EVENT TRIAGE:\n"
        "- LANDMARK (significant for weeks — PRs, milestones, injuries, major decisions): [list or 'none']\n"
        "- SIGNAL (pattern worth addressing — recurring behavior, multi-week trend, athlete question): [list or 'none']\n"
        "- NOISE (one-off disruption — travel miss, minor scheduling — acknowledge briefly, don't dwell): [list or 'none']\n\n"
        "OPEN FOLLOW-UPS CHECK: [From your watch list — what needs checking today? What might be resolved? What is stale?]\n\n"
        "WHAT MATTERS TODAY: [1-2 things max — be ruthlessly selective. The athlete's attention is limited.]\n\n"
        "COACH'S OWN AGENDA: [What will you push today independent of athlete input? Think: ignored trends, "
        "long-term phase, health factors he's not tracking (sleep, carbs, VO2 max), follow-ups due.]\n\n"
        "FOCUS UPDATES NEEDED: [Any new items to start tracking? Anything resolved? "
        "Format: TRACKING/LANDMARK/FOLLOWUP/RESOLVED: description]\n\n"
        "Be direct and honest. This is your internal thinking only."
    )

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=700,
        system=system_prompt,
        messages=[
            {"role": "user", "content": user_message + f"\n\n---\n\n{analysis_request}"}
        ]
    )
    return message.content[0].text


def generate_email(system_prompt: str, user_message: str, analysis: str = "") -> str:
    """Send the prompt to Claude and return the email text."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    full_user_message = user_message
    if analysis:
        full_user_message += (
            f"\n\n---\n\nYOUR PRE-ANALYSIS\n{analysis}"
            "\n\n---\n\nNow write the coaching email based on your analysis above."
        )

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=system_prompt,
        messages=[
            {"role": "user", "content": full_user_message}
        ]
    )

    return message.content[0].text


# ---------------------------------------------------------------------------
# Extract [TELEGRAM: ...] proactive alert from email text
# ---------------------------------------------------------------------------

def extract_telegram_alert(email_text: str) -> tuple[str, str]:
    """
    Look for [TELEGRAM: message] at the end of the email.
    Returns (clean_email_text, telegram_message).
    The marker is stripped from the email before sending.
    """
    import re
    pattern = r'\[TELEGRAM:\s*(.*?)\]'
    match = re.search(pattern, email_text, re.IGNORECASE | re.DOTALL)
    if match:
        tg_msg = match.group(1).strip()
        clean = re.sub(pattern, '', email_text, flags=re.IGNORECASE | re.DOTALL).strip()
        return clean, tg_msg
    return email_text, ""


# ---------------------------------------------------------------------------
# Coach focus markers: parse + write back to Coach Memory
# ---------------------------------------------------------------------------

def parse_coach_focus_markers(email_text: str) -> tuple[str, list[dict]]:
    """
    Extract [TRACKING: ...], [LANDMARK: ...], [FOLLOWUP: ...], [RESOLVED: ...] markers.
    Returns (clean_email_text, list of {category, item} dicts).
    Markers are stripped from the email before the athlete sees it.
    """
    import re
    markers = []
    categories = ["TRACKING", "LANDMARK", "FOLLOWUP", "CONCERN", "RESOLVED"]
    clean = email_text
    for cat in categories:
        pattern = rf'\[{cat}:\s*(.*?)\]'
        for match in re.finditer(pattern, email_text, re.IGNORECASE | re.DOTALL):
            markers.append({"category": cat, "item": match.group(1).strip()})
        clean = re.sub(pattern, '', clean, flags=re.IGNORECASE | re.DOTALL)
    return clean.strip(), markers


def write_coach_focus_updates(updates: list[dict]) -> None:
    """Write coach focus marker updates to Coach Memory (non-fatal)."""
    if not updates:
        return
    try:
        from memory import append_coach_focus, update_coach_focus_status
        today = str(date.today())
        for u in updates:
            category = u["category"]
            item = u["item"]
            if category == "RESOLVED":
                found = update_coach_focus_status(item, "RESOLVED", last_mentioned=today)
                if not found:
                    print(f"    [Focus] RESOLVED marker didn't match any open item: '{item[:60]}'")
            else:
                append_coach_focus(category, item, last_mentioned=today)
                print(f"    [Focus] {category}: {item[:80]}")
    except Exception as e:
        print(f"  Coach focus update failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Weekly recap day — reads from Athlete Preferences, falls back to Sunday
# ---------------------------------------------------------------------------

_DAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2,
    "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6,
}

def _get_recap_weekday(athlete_prefs: list[dict]) -> int:
    """
    Read the preferred weekly recap day from Athlete Preferences.
    Looks for a SCHEDULE preference containing 'weekly_recap_day'.
    Returns weekday int (0=Monday … 6=Sunday). Default: 6 (Sunday).
    """
    for pref in athlete_prefs:
        if pref.get("Category", "").upper() != "SCHEDULE":
            continue
        text = pref.get("Preference", "").lower()
        if "weekly_recap_day" not in text:
            continue
        # Expected format: "weekly_recap_day: Sunday" or "weekly recap day: friday"
        parts = text.split(":")
        if len(parts) >= 2:
            day_str = parts[-1].strip()
            if day_str in _DAY_NAMES:
                return _DAY_NAMES[day_str]
    return 6  # default: Sunday


# ---------------------------------------------------------------------------
# Plateau detection: find stalled lifts and run deep dives
# ---------------------------------------------------------------------------

def detect_plateaus_and_deep_dive(lift_history: list[dict], system_prompt: str,
                                   tracked_lifts: list[dict] = None) -> dict:
    """
    Check 1RM trajectory for each key lift. If plateaued, fetch full history
    and run a focused analysis. Returns {lift_name: analysis_text}.
    Only checks MAIN lifts (plateau detection is for primary lifts only).
    """
    from planner import run_lift_deep_dive
    from memory import read_lift_history_for_exercise

    plateau_dives = {}

    # Use dynamic tracked lifts (MAIN only), fall back to KEY_LIFTS
    if tracked_lifts:
        lifts_to_check = [(tl["domain"], tl["match_pattern"]) for tl in tracked_lifts
                          if tl.get("lift_type", "MAIN") == "MAIN"]
    else:
        lifts_to_check = KEY_LIFTS

    for _domain, lift in lifts_to_check:
        readings = []
        for row in lift_history:
            if lift.lower() not in row.get("Exercise", "").lower():
                continue
            est = row.get("Est 1RM", "")
            if not est:
                continue
            try:
                readings.append(float(est))
            except (ValueError, TypeError):
                pass

        if len(readings) < 3:
            continue

        recent = readings[-3:]
        spread = max(recent) - min(recent)
        if max(recent) > 0 and spread / max(recent) < 0.01:
            print(f"    → Plateau detected for {lift}, running deep dive...")
            full_history = read_lift_history_for_exercise(lift)
            analysis = run_lift_deep_dive(lift, full_history, system_prompt)
            if analysis:
                plateau_dives[lift] = analysis

    return plateau_dives


# ---------------------------------------------------------------------------
# Coach State writer — pure Python, no LLM calls
# ---------------------------------------------------------------------------

def write_coach_state_summaries(
    memory_data: dict,
    projections: dict,
    program_data: dict,
    week_num: int,
    dry_run: bool = False,
) -> None:
    """
    Write compressed domain summaries to the Coach State tab.
    Called at the end of each run so next run starts from a bounded context.
    Pure Python — uses projection data + program data, no LLM call.
    """
    try:
        from memory import upsert_coach_state

        # --- PROGRAM domain ---
        prog_proj = projections.get("program_projection")
        if prog_proj:
            prog_summary = (
                f"Week {prog_proj['week_num']}/{prog_proj['total_weeks']} "
                f"({prog_proj['pct_complete']}% complete, {prog_proj['weeks_remaining']} weeks left, "
                f"ends {prog_proj['estimated_end_date']})"
            )
            _write_state(upsert_coach_state, "PROGRAM", prog_summary, "HIGH", dry_run)

        # --- Lift domains (MAIN lifts from tracked_lifts registry, fallback KEY_LIFTS) ---
        tracked_lifts = memory_data.get("tracked_lifts")
        main_lifts = [(tl["domain"], tl["match_pattern"]) for tl in tracked_lifts
                      if tl.get("lift_type", "MAIN") == "MAIN"] if tracked_lifts else KEY_LIFTS
        lift_proj_map = {p["exercise"].upper(): p for p in projections.get("lift_projections", []) if p}
        for domain, lift_name in main_lifts:
            proj = lift_proj_map.get(domain) or lift_proj_map.get(lift_name.upper())
            if not proj:
                continue
            curr = proj["current_1rm"]
            rate = proj["rate_per_week"]
            end_proj = proj.get("projected_end_1rm")
            on_track = proj.get("on_track")
            target = proj.get("target_1rm")

            parts = [f"est 1RM {curr}kg", f"trend {rate:+.2f}kg/wk"]
            if end_proj is not None:
                parts.append(f"projected end: {end_proj}kg")
            if target:
                parts.append(f"target: {target}kg")
            if on_track is True:
                parts.append("ON TRACK")
            elif on_track is False:
                wtt = proj.get("weeks_to_target")
                wr = prog_proj["weeks_remaining"] if prog_proj else None
                if wtt and wr:
                    parts.append(f"BEHIND ({wtt:.0f}wk needed, {wr}wk left)")
                else:
                    parts.append("BEHIND TARGET")

            confidence = "HIGH" if proj.get("data_points", 0) >= 6 else "MEDIUM"
            _write_state(upsert_coach_state, domain, " | ".join(parts), confidence, dry_run)

        # --- HEALTH domain ---
        bw_proj = projections.get("bw_projection")
        health_log = memory_data.get("health_log", [])
        health_parts = []
        if bw_proj:
            health_parts.append(
                f"BW {bw_proj['current_bw']}kg | trend {bw_proj['rate_per_week']:+.2f}kg/wk "
                f"({bw_proj['trend_direction']}) | 2wk avg {bw_proj['2wk_avg']}kg"
            )
        if health_log:
            recent = health_log[-14:]
            sleep_vals = []
            for e in recent:
                try:
                    sleep_vals.append(float(e.get("Sleep (hrs)", "") or ""))
                except (ValueError, TypeError):
                    pass
            if sleep_vals:
                health_parts.append(f"sleep avg {sum(sleep_vals)/len(sleep_vals):.1f}h (14d)")
        if health_parts:
            _write_state(upsert_coach_state, "HEALTH", " | ".join(health_parts),
                         "HIGH" if bw_proj else "MEDIUM", dry_run)

        # --- SCHEDULE domain ---
        current_week = program_data.get("current_week", {})
        sessions = current_week.get("sessions", [])
        if sessions:
            done = sum(1 for s in sessions if s.get("completed"))
            total = len(sessions)
            sched_summary = f"Week {week_num}: {done}/{total} days completed"
            _write_state(upsert_coach_state, "SCHEDULE", sched_summary, "HIGH", dry_run)

    except Exception as e:
        print(f"  Coach State write failed (non-fatal): {e}")


def _write_state(fn, domain: str, summary: str, confidence: str, dry_run: bool) -> None:
    if dry_run:
        print(f"    [DRY RUN] Coach State | {domain}: {summary[:100]}")
    else:
        fn(domain, summary, confidence)
        print(f"    [Coach State] {domain}: {summary[:80]}")


# ---------------------------------------------------------------------------
# Write-back: check if agent wants to propose a program change
# ---------------------------------------------------------------------------

def check_for_write_back_proposals(email_text: str) -> str:
    """
    Look for the agent's proposal pattern at the end of the email.
    Pattern: "One thing: [proposal]. Want me to update the sheet?"

    Returns the proposal text if found, empty string otherwise.
    """
    lower = email_text.lower()
    if "want me to update the sheet" in lower or "want me to update the program" in lower:
        sentences = email_text.replace("\n", " ").split(".")
        for s in sentences:
            if "want me to update" in s.lower():
                return s.strip()
    return ""


def log_pending_proposal(proposal_text: str, existing_commands: list[dict]) -> None:
    """
    Log a write-back proposal to the Commands tab so it persists to the next run.
    Skips if an identical or very similar PENDING_PROPOSAL already exists.
    """
    # Check for duplicates — avoid re-logging the same proposal
    proposal_lower = proposal_text.lower()[:80]
    for cmd in existing_commands:
        if cmd.get("Command", "").upper() == "PENDING_PROPOSAL":
            if cmd.get("Applied", "").upper() != "Y":
                existing_val = cmd.get("Value", "").lower()[:80]
                # Simple similarity: if 60%+ of words overlap, treat as duplicate
                words_new = set(proposal_lower.split())
                words_old = set(existing_val.split())
                if words_new and words_old:
                    overlap = len(words_new & words_old) / len(words_new | words_old)
                    if overlap > 0.6:
                        return  # already logged

    try:
        from memory import append_command
        append_command("PENDING_PROPOSAL", proposal_text)
        print(f"    [Proposal logged to Commands]: {proposal_text[:80]}")
    except Exception as e:
        print(f"    Proposal logging failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run_think(week_num: int = None, dry_run: bool = False):
    """Run the strategic planning pass only. No email sent."""
    from sheets import read_program_data
    from memory import read_all
    from planner import run_planning_pass

    if week_num is None:
        week_num = compute_current_week(resolve_program_start_date())

    today = date.today()
    print(f"[{today}] Running strategic planning pass for Week {week_num}...")

    program_data = read_program_data(week_num=week_num)
    memory_data = read_all()
    run_planning_pass(program_data, memory_data, week_num, dry_run=dry_run)
    print("Planning pass complete.")


def run(week_num: int = None, dry_run: bool = False, no_sync: bool = False,
        force_weekly: bool = False):
    from sheets import read_program_data
    from memory import (read_all, sync_sessions_to_history, sync_health_log,
                        log_coach_run, get_last_run_date, check_skip_today,
                        expire_stale_focus_items)
    from prompt import build_prompt
    from gmail import read_recent_replies

    # Auto-compute week if not overridden
    if week_num is None:
        week_num = compute_current_week(resolve_program_start_date())

    today = date.today()
    print(f"[{today}] Running coach for Week {week_num}...")

    # --- Check for skip command ---
    skip_until = check_skip_today()
    if skip_until and not dry_run:
        print(f"  SKIP_UNTIL command active — no email until {skip_until}. Exiting.")
        return None

    # 1. Expire stale Coach Focus items (Priority-aware: PINNED=never, HIGH=90d, NORMAL=30d)
    try:
        expired = expire_stale_focus_items()
        if expired:
            print(f"  → {expired} stale focus item(s) expired")
    except Exception as e:
        print(f"  Stale focus expiry failed (non-fatal): {e}")

    # 2. Process unprocessed Telegram messages (classify → structured facts → memory)
    print("  Processing Telegram messages...")
    try:
        from processor import process_telegram_messages
        process_telegram_messages(dry_run=dry_run)
    except Exception as e:
        print(f"  Telegram processor failed (non-fatal): {e}")

    # 3. Read program sheet
    print("  Reading program sheet...")
    program_data = read_program_data(week_num=week_num)

    # 4. Read coach memory
    print("  Reading coach memory...")
    memory_data = read_all()

    # --- Determine email type (after memory load so we can read Athlete Preferences) ---
    # Recap day is read from Athlete Preferences (SCHEDULE | weekly_recap_day: <dayname>)
    # Falls back to Sunday (weekday 6). Athlete can change this via Telegram.
    recap_weekday = _get_recap_weekday(memory_data.get("athlete_preferences", []))
    is_weekly_summary = force_weekly or (today.weekday() == recap_weekday)
    if is_weekly_summary:
        day_name = ["Monday", "Tuesday", "Wednesday", "Thursday",
                    "Friday", "Saturday", "Sunday"][recap_weekday]
        print(f"  Weekly summary mode ({day_name} or --weekly flag).")

    # 5. Compute projections (pure Python — facts for prompt + Coach State)
    print("  Computing projections...")
    try:
        from projections import run_all_projections
        projections = run_all_projections(memory_data)
        if projections.get("formatted"):
            print(f"    → {projections['formatted'].count(chr(10)) + 1} projection line(s) computed")
    except Exception as e:
        print(f"  Projections failed (non-fatal): {e}")
        projections = {}

    # 5. Get last run date for delta detection
    last_run_date = get_last_run_date()
    if last_run_date:
        print(f"  Last email: {last_run_date} — computing delta...")

    # 6. Read email replies (since last run)
    print("  Checking for email replies...")
    replies = read_recent_replies(after_date=last_run_date, max_results=5)
    if replies:
        print(f"    → {len(replies)} reply(ies) found")

    # 7. Sync new data to memory (unless --no-sync)
    if not no_sync:
        print("  Syncing new session data to history...")
        new_sessions = sync_sessions_to_history(program_data)
        if new_sessions:
            print(f"    → {len(new_sessions)} new exercise completions logged")

        print("  Syncing health log...")
        new_health = sync_health_log(program_data)
        if new_health:
            print(f"    → {len(new_health)} new health entries logged")

    # 8. Build prompt (initial pass, without plateau dives)
    print("  Building prompt...")
    system_prompt, user_message = build_prompt(
        program_data, memory_data,
        last_run_date=last_run_date,
        replies=replies,
        is_weekly_summary=is_weekly_summary,
        projections_text=projections.get("formatted", ""),
    )

    # 9. Plateau detection + per-lift deep dives
    print("  Checking for plateaus...")
    lift_history = memory_data.get("lift_history", [])
    plateau_dives = detect_plateaus_and_deep_dive(
        lift_history, system_prompt, tracked_lifts=memory_data.get("tracked_lifts"))
    if plateau_dives:
        # Rebuild prompt with deep dive context included
        system_prompt, user_message = build_prompt(
            program_data, memory_data,
            last_run_date=last_run_date,
            replies=replies,
            is_weekly_summary=is_weekly_summary,
            plateau_deep_dives=plateau_dives,
            projections_text=projections.get("formatted", ""),
        )

    # 10. Analysis pass (reasoning before writing)
    print("  Running analysis pass...")
    analysis = generate_analysis(system_prompt, user_message)
    if dry_run:
        print("\n--- ANALYSIS ---")
        print(analysis)
        print("--- END ANALYSIS ---\n")

    # 9. Generate email
    print("  Generating email with Claude...")
    email_text = generate_email(system_prompt, user_message, analysis=analysis)

    # 11. Extract output markers (Telegram alert + coach focus updates)
    email_text, tg_alert = extract_telegram_alert(email_text)
    if tg_alert:
        print(f"  [Telegram alert detected]: {tg_alert}")

    email_text, focus_updates = parse_coach_focus_markers(email_text)
    if focus_updates:
        print(f"  [Coach focus updates]: {len(focus_updates)} item(s)")
        if not no_sync and not dry_run:
            write_coach_focus_updates(focus_updates)
        elif dry_run:
            for u in focus_updates:
                print(f"    [DRY RUN] {u['category']}: {u['item'][:80]}")

    # 12. Check for write-back proposals — log to Commands so they persist
    proposal = check_for_write_back_proposals(email_text)
    if proposal:
        print(f"\n  [Write-back proposal detected]: {proposal}")
        print("  → Logging to Commands tab — will check for confirmation next run.")
        if not dry_run and not no_sync:
            existing_commands = memory_data.get("commands", [])
            log_pending_proposal(proposal, existing_commands)

    # 13. Generate charts (on Fridays / weekly summary)
    charts = None
    if is_weekly_summary:
        print("  Generating charts...")
        try:
            from charts import generate_1rm_chart, generate_volume_chart, generate_bodyweight_chart
            chart_list = []
            c1 = generate_1rm_chart(memory_data.get("lift_history", []),
                                    tracked_lifts=memory_data.get("tracked_lifts"))
            if c1:
                chart_list.append((c1, "chart-1rm"))
            c2 = generate_volume_chart(
                program_data.get("recent_weeks", []),
                program_data.get("current_week"),
            )
            if c2:
                chart_list.append((c2, "chart-volume"))
            c3 = generate_bodyweight_chart(memory_data.get("health_log", []))
            if c3:
                chart_list.append((c3, "chart-bw"))
            charts = chart_list if chart_list else None
            print(f"    → {len(chart_list)} chart(s) generated")
        except ImportError:
            print("    → matplotlib not installed, skipping charts")

    # 14. Output
    if dry_run:
        print("\n" + "=" * 60)
        print(f"COACHING EMAIL — {today}")
        print("=" * 60)
        print(email_text)
        if charts:
            print(f"\n[{len(charts)} chart(s) would be attached inline]")
        if tg_alert:
            print(f"\n[Telegram alert would send]: {tg_alert}")
        print("=" * 60)
        print("[DRY RUN — email not sent]")
    else:
        from gmail import send_email
        week_label = f"Week {week_num}"
        if is_weekly_summary:
            subject = f"{week_label} — Weekly Summary — {today.strftime('%b %d')}"
        else:
            subject = f"{week_label} — {today.strftime('%b %d')}"
        print(f"  Sending email: '{subject}'...")
        send_email(subject=subject, body=email_text, charts=charts or [])
        print("  Email sent.")

        # Send proactive Telegram alert if coach flagged one
        if tg_alert:
            try:
                from telegram_utils import send_telegram_message
                send_telegram_message(tg_alert)
                print(f"  Telegram alert sent: {tg_alert[:80]}")
            except Exception as e:
                print(f"  Telegram alert failed (non-fatal): {e}")

    # 15. Write Coach State summaries (bounded Tier 1 memory for next run)
    print("  Writing Coach State summaries...")
    write_coach_state_summaries(
        memory_data=memory_data,
        projections=projections,
        program_data=program_data,
        week_num=week_num,
        dry_run=dry_run or no_sync,
    )

    # 16. Log the run to Coach Memory
    if not no_sync and not dry_run:
        first_sentence = email_text.split(".")[0].strip()
        log_coach_run(
            observations=first_sentence[:200],
            email_summary=email_text[:500],
        )
        print("  Run logged to Coach Memory.")

    return email_text


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = parse_args()

    if args.setup:
        from memory import setup_memory_sheet
        print("Setting up Coach Memory Sheet...")
        setup_memory_sheet()
        sys.exit(0)

    week_num = args.week or None

    try:
        if args.think:
            run_think(week_num=week_num, dry_run=args.dry_run)
        else:
            run(
                week_num=week_num,
                dry_run=args.dry_run,
                no_sync=args.no_sync,
                force_weekly=args.weekly,
            )
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        raise
