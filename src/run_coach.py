"""
Main entry point for the strength coach agent.

Usage:
  python src/run_coach.py              # Full run: analyze + send email
  python src/run_coach.py --dry-run    # Analyze + print email, do not send
  python src/run_coach.py --week 8     # Override current week number
  python src/run_coach.py --setup      # Set up Coach Memory Sheet (first-time only)
  python src/run_coach.py --no-sync    # Skip writing new data to Coach Memory (read-only)
  python src/run_coach.py --weekly     # Force a weekly summary email with charts
"""

import argparse
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8")

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, compute_current_week, PROGRAM_START_DATE


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
                        help="Force a weekly summary email with charts (normally auto on Fridays)")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Claude calls
# ---------------------------------------------------------------------------

def generate_analysis(system_prompt: str, user_message: str) -> str:
    """
    First pass: ask Claude to produce a brief structured analysis before writing.
    This is the 'thinking' step — it doesn't go in the email, it informs it.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    analysis_request = (
        "Before writing the coaching email, produce a brief structured pre-analysis:\n\n"
        "OBSERVATIONS: [2-3 key observations from the data]\n"
        "CONCERNS: [red flags or issues worth addressing — be honest]\n"
        "QUESTIONS: [topics from athlete notes/replies that need answering]\n"
        "TRAJECTORY: [brief assessment of progress pace toward goals]\n"
        "FOCUS: [1-2 things most worth addressing in today's email]\n\n"
        "Be concise and direct. This is your internal thinking, not the email itself."
    )

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=600,
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


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(week_num: int = None, dry_run: bool = False, no_sync: bool = False,
        force_weekly: bool = False):
    from sheets import read_program_data
    from memory import (read_all, sync_sessions_to_history, sync_health_log,
                        log_coach_run, get_last_run_date, check_skip_today)
    from prompt import build_prompt
    from gmail import read_recent_replies

    # Auto-compute week if not overridden
    if week_num is None:
        week_num = compute_current_week(PROGRAM_START_DATE)

    today = date.today()
    print(f"[{today}] Running coach for Week {week_num}...")

    # --- Check for skip command ---
    skip_until = check_skip_today()
    if skip_until and not dry_run:
        print(f"  SKIP_UNTIL command active — no email until {skip_until}. Exiting.")
        return None

    # --- Determine email type ---
    is_friday = today.weekday() == 4  # 0=Monday, 4=Friday
    is_weekly_summary = force_weekly or is_friday
    if is_weekly_summary:
        print("  Weekly summary mode (Friday or --weekly flag).")

    # 1. Read program sheet
    print("  Reading program sheet...")
    program_data = read_program_data(week_num=week_num)

    # 2. Read coach memory
    print("  Reading coach memory...")
    memory_data = read_all()

    # 3. Get last run date for delta detection
    last_run_date = get_last_run_date()
    if last_run_date:
        print(f"  Last email: {last_run_date} — computing delta...")

    # 4. Read email replies (since last run)
    print("  Checking for email replies...")
    replies = read_recent_replies(after_date=last_run_date, max_results=5)
    if replies:
        print(f"    → {len(replies)} reply(ies) found")

    # 5. Sync new data to memory (unless --no-sync)
    if not no_sync:
        print("  Syncing new session data to history...")
        new_sessions = sync_sessions_to_history(program_data)
        if new_sessions:
            print(f"    → {len(new_sessions)} new exercise completions logged")

        print("  Syncing health log...")
        new_health = sync_health_log(program_data)
        if new_health:
            print(f"    → {len(new_health)} new health entries logged")

    # 6. Build prompt
    print("  Building prompt...")
    system_prompt, user_message = build_prompt(
        program_data, memory_data,
        last_run_date=last_run_date,
        replies=replies,
        is_weekly_summary=is_weekly_summary,
    )

    # 7. Analysis pass (reasoning before writing)
    print("  Running analysis pass...")
    analysis = generate_analysis(system_prompt, user_message)
    if dry_run:
        print("\n--- ANALYSIS ---")
        print(analysis)
        print("--- END ANALYSIS ---\n")

    # 8. Generate email
    print("  Generating email with Claude...")
    email_text = generate_email(system_prompt, user_message, analysis=analysis)

    # 9. Check for write-back proposals
    proposal = check_for_write_back_proposals(email_text)
    if proposal:
        print(f"\n  [Write-back proposal detected]: {proposal}")
        print("  → User must confirm via daily notes before any changes are applied.")

    # 10. Generate charts (on Fridays / weekly summary)
    charts = None
    if is_weekly_summary:
        print("  Generating charts...")
        try:
            from charts import generate_1rm_chart, generate_volume_chart, generate_bodyweight_chart
            chart_list = []
            c1 = generate_1rm_chart(memory_data.get("lift_history", []))
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

    # 11. Output
    if dry_run:
        print("\n" + "=" * 60)
        print(f"COACHING EMAIL — {today}")
        print("=" * 60)
        print(email_text)
        if charts:
            print(f"\n[{len(charts)} chart(s) would be attached inline]")
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

    # 12. Log the run to Coach Memory
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
