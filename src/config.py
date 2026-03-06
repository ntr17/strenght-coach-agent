import math
import os
import re
from datetime import date, datetime
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")


def _extract_sheet_id(value: str) -> str:
    """Accept either a bare Sheet ID or a full Google Sheets URL."""
    if not value:
        return value
    # Extract ID from URL like: .../spreadsheets/d/SHEET_ID/edit...
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", value)
    return match.group(1) if match else value.strip()


def compute_current_week(start_date_str: str, today: date = None) -> int:
    """
    Compute the current training week number from the program start date.
    Week 1 = days 1-7, Week 2 = days 8-14, etc.
    Returns at minimum 1, no upper bound (program may exceed original length).
    """
    if today is None:
        today = date.today()
    try:
        start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return 1
    days_elapsed = (today - start).days
    return max(1, math.ceil((days_elapsed + 1) / 7))


ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
# PROGRAM_SHEET_ID is optional — agent falls back to Coach Memory registry if absent.
PROGRAM_SHEET_ID = _extract_sheet_id(os.environ.get("PROGRAM_SHEET_ID", ""))
MEMORY_SHEET_ID = _extract_sheet_id(os.environ["MEMORY_SHEET_ID"])
GMAIL_FROM = os.environ["GMAIL_FROM"]
GMAIL_TO = os.environ["GMAIL_TO"]
ATHLETE_NAME = os.environ.get("ATHLETE_NAME", "Nacho")
PROGRAM_START_DATE = os.environ.get("PROGRAM_START_DATE", "2026-01-13")
# CURRENT_WEEK env var is an optional manual override; normally computed from date.
_CURRENT_WEEK_OVERRIDE = os.environ.get("CURRENT_WEEK", "")
CURRENT_WEEK = int(_CURRENT_WEEK_OVERRIDE) if _CURRENT_WEEK_OVERRIDE else compute_current_week(PROGRAM_START_DATE)
EMAIL_HOUR = int(os.environ.get("EMAIL_HOUR", "22"))  # 10 PM default

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
TOKEN_FILE = CONFIG_DIR / "token.json"

# Google API scopes needed
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.send",
]

# Claude model
CLAUDE_MODEL = "claude-sonnet-4-6"
