"""
Gmail sender using the Gmail API with OAuth2.
Reuses the same credentials as Google Sheets (single OAuth flow).
"""

import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from googleapiclient.discovery import build

from sheets import get_credentials
from config import GMAIL_FROM, GMAIL_TO


def _build_html(text: str) -> str:
    """Convert plain text email to simple HTML (preserves line breaks)."""
    # Replace double newlines with paragraph breaks, single with <br>
    paragraphs = text.strip().split("\n\n")
    html_parts = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        # Within a paragraph, replace single newlines with <br>
        para_html = para.replace("\n", "<br>")
        html_parts.append(f"<p>{para_html}</p>")

    return f"""<!DOCTYPE html>
<html>
<head>
<style>
  body {{ font-family: Georgia, serif; font-size: 15px; line-height: 1.6;
          color: #1a1a1a; max-width: 620px; margin: 40px auto; padding: 0 20px; }}
  p {{ margin: 0 0 1em 0; }}
</style>
</head>
<body>
{"".join(html_parts)}
</body>
</html>"""


def send_email(subject: str, body: str,
               to: str = None, from_addr: str = None) -> dict:
    """
    Send an email via Gmail API.

    Args:
        subject: Email subject line
        body: Plain text email body (will be sent as both plain text and HTML)
        to: Recipient address (defaults to GMAIL_TO from config)
        from_addr: Sender address (defaults to GMAIL_FROM from config)

    Returns:
        Gmail API response dict
    """
    to = to or GMAIL_TO
    from_addr = from_addr or GMAIL_FROM

    # Build MIME message
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to

    # Attach plain text part
    msg.attach(MIMEText(body, "plain", "utf-8"))

    # Attach HTML part
    html_body = _build_html(body)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Encode
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    # Send via Gmail API
    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds)
    result = service.users().messages().send(
        userId="me",
        body={"raw": raw}
    ).execute()

    return result


if __name__ == "__main__":
    # Quick test: send a test email
    print(f"Sending test email to {GMAIL_TO}...")
    result = send_email(
        subject="Coach Agent — Test Email",
        body="This is a test email from your strength coach agent. If you received this, Gmail sending is working correctly."
    )
    print(f"Sent. Message ID: {result.get('id')}")
