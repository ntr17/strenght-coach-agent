"""
Main entry point for the strength coach agent.

Usage:
  python src/run_coach.py              # Full run: analyze + send email
  python src/run_coach.py --dry-run    # Analyze + print email, do not send
  python src/run_coach.py --week 8     # Override current week number
  python src/run_coach.py --setup      # Set up Coach Memory Sheet (first-time only)
"""

import argparse
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8")

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, ATHLETE_NAME, CURRENT_WEEK


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
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Call Claude
# ---------------------------------------------------------------------------

def generate_email(system_prompt: str, user_message: str) -> str:
    """Send the prompt to Claude and return the email text."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=system_prompt,
        messages=[
            {"role": "user", "content": user_message}
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
        # Extract the proposal — it's the sentence containing this phrase
        sentences = email_text.replace("\n", " ").split(".")
        for s in sentences:
            if "want me to update" in s.lower():
                return s.strip()
    return ""


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(week_num: int, dry_run: bool = False, no_sync: bool = False):
    from sheets import read_program_data
    from memory import read_all, sync_sessions_to_history, sync_health_log, log_coach_run
    from prompt import build_prompt

    print(f"[{date.today()}] Running coach for Week {week_num}...")

    # 1. Read program sheet
    print("  Reading program sheet...")
    program_data = read_program_data(week_num=week_num)

    # 2. Read coach memory
    print("  Reading coach memory...")
    memory_data = read_all()

    # 3. Sync new data to memory (unless --no-sync)
    if not no_sync:
        print("  Syncing new session data to history...")
        new_sessions = sync_sessions_to_history(program_data)
        if new_sessions:
            print(f"    → {len(new_sessions)} new exercise completions logged")

        print("  Syncing health log...")
        new_health = sync_health_log(program_data)
        if new_health:
            print(f"    → {len(new_health)} new health entries logged")

    # 4. Build prompt
    print("  Building prompt...")
    system_prompt, user_message = build_prompt(program_data, memory_data)

    # 5. Generate email
    print("  Generating email with Claude...")
    email_text = generate_email(system_prompt, user_message)

    # 6. Check for write-back proposals
    proposal = check_for_write_back_proposals(email_text)
    if proposal:
        print(f"\n  [Write-back proposal detected]: {proposal}")
        print("  → User must confirm via daily notes before any changes are applied.")

    # 7. Output
    if dry_run:
        print("\n" + "=" * 60)
        print(f"COACHING EMAIL — {date.today()}")
        print("=" * 60)
        print(email_text)
        print("=" * 60)
        print("[DRY RUN — email not sent]")
    else:
        from gmail import send_email
        subject = f"Week {week_num} — {date.today().strftime('%b %d')}"
        print(f"  Sending email: '{subject}'...")
        send_email(subject=subject, body=email_text)
        print("  Email sent.")

    # 8. Log the run to Coach Memory
    if not no_sync and not dry_run:
        # Derive a one-line observation summary from the email
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

    week_num = args.week or CURRENT_WEEK

    try:
        run(
            week_num=week_num,
            dry_run=args.dry_run,
            no_sync=args.no_sync,
        )
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        raise
