import os
import re
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


ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
PROGRAM_SHEET_ID = _extract_sheet_id(os.environ["PROGRAM_SHEET_ID"])
MEMORY_SHEET_ID = _extract_sheet_id(os.environ["MEMORY_SHEET_ID"])
GMAIL_FROM = os.environ["GMAIL_FROM"]
GMAIL_TO = os.environ["GMAIL_TO"]
ATHLETE_NAME = os.environ.get("ATHLETE_NAME", "Nacho")
CURRENT_WEEK = int(os.environ.get("CURRENT_WEEK", "1"))
PROGRAM_START_DATE = os.environ.get("PROGRAM_START_DATE", "2026-01-13")
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
